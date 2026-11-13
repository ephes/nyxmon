"""Deletion must confirm the current definition, including a service's children."""

import pytest
from django.test import Client
from django.urls import reverse

from nyxboard.models import HealthCheck, Result, Service

pytestmark = pytest.mark.django_db


def revision(client, url):
    response = client.get(url)
    assert response.status_code == 200
    return response.context.get("definition_revision", "unprotected-old-form")


@pytest.mark.parametrize("kind", ["service", "healthcheck"])
@pytest.mark.parametrize("change", ["name", "config", "move", "add", "remove"])
def test_stale_delete_preserves_rows_and_results(
    client, service_factory, healthcheck_factory, result_factory, kind, change
):
    service = service_factory()
    check = healthcheck_factory(service, name="synthetic")
    result = result_factory(check)
    other = service_factory(name="other")
    second = Client()
    url = reverse(
        f"nyxboard:{kind}_delete", args=[service.pk if kind == "service" else check.pk]
    )
    token = revision(client, url)
    if change == "name":
        if kind == "service":
            second.post(
                reverse("nyxboard:service_update", args=[service.pk]),
                {
                    "name": "changed",
                    "definition_revision": second.get(
                        reverse("nyxboard:service_update", args=[service.pk])
                    )
                    .context["form"]["definition_revision"]
                    .value(),
                },
            )
        else:
            HealthCheck.objects.filter(pk=check.pk).update(name="changed")
    elif change == "config":
        HealthCheck.objects.filter(pk=check.pk).update(
            data={"host": "synthetic.invalid"}
        )
    elif change == "move":
        HealthCheck.objects.filter(pk=check.pk).update(service=other)
    elif change == "add":
        if kind == "healthcheck":
            HealthCheck.objects.filter(pk=check.pk).update(disabled=True)
        else:
            healthcheck_factory(service, name="new child")
    elif change == "remove":
        # A service's child-set changes without deleting the evidence check.
        if kind == "healthcheck":
            HealthCheck.objects.filter(pk=check.pk).update(check_interval=60)
        else:
            child = healthcheck_factory(service, name="removed child")
            token = revision(client, url)
            child.delete()
    response = client.post(url, {"definition_revision": token})
    assert response.status_code == 409
    assert b"reload" in response.content.lower()
    assert Service.objects.filter(pk=service.pk).exists()
    assert HealthCheck.objects.filter(pk=check.pk).exists()
    assert Result.objects.filter(pk=result.pk).exists()
    # A fresh, deliberate confirmation can delete the new current definition.
    fresh = revision(second, url)
    assert second.post(url, {"definition_revision": fresh}).status_code == 302


@pytest.mark.parametrize("kind", ["service", "healthcheck"])
def test_runtime_updates_do_not_invalidate_delete(
    client, service_factory, healthcheck_factory, result_factory, kind
):
    service = service_factory()
    check = healthcheck_factory(service)
    url = reverse(
        f"nyxboard:{kind}_delete", args=[service.pk if kind == "service" else check.pk]
    )
    token = revision(client, url)
    HealthCheck.objects.filter(pk=check.pk).update(
        status="processing", processing_started_at=123, next_check_time=456
    )
    result_factory(check)
    assert client.post(url, {"definition_revision": token}).status_code == 302
    assert not HealthCheck.objects.filter(pk=check.pk).exists()


@pytest.mark.parametrize("kind", ["service", "healthcheck"])
@pytest.mark.parametrize("token", [None, "tampered"])
def test_missing_or_invalid_confirmation_is_a_conflict(
    client, service_factory, healthcheck_factory, kind, token
):
    service = service_factory()
    check = healthcheck_factory(service)
    url = reverse(
        f"nyxboard:{kind}_delete", args=[service.pk if kind == "service" else check.pk]
    )
    response = client.post(url, {} if token is None else {"definition_revision": token})
    assert response.status_code == 409
    assert HealthCheck.objects.filter(pk=check.pk).exists()
