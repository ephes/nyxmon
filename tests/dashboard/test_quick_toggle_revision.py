"""Quick mutations require the same current definition as full edits."""

import json

from bs4 import BeautifulSoup
import pytest
from django.db.models.query import QuerySet
from django.urls import reverse

from nyxboard.forms import HealthCheckForm
from nyxboard.models import HealthCheck

pytestmark = pytest.mark.django_db


@pytest.fixture
def check(service_factory, healthcheck_factory):
    return healthcheck_factory(service_factory(), next_check_time=9000)


def revision(check):
    return HealthCheckForm(instance=check)["definition_revision"].value()


def post(client, check, path, token):
    data = {"definition_revision": token, "csrfmiddlewaretoken": "synthetic"}
    if path == "healthcheck_update":
        data["disabled"] = "1"
    return client.post(reverse(f"nyxboard:{path}", args=[check.pk]), data)


@pytest.mark.parametrize("path", ["healthcheck_update", "healthcheck_toggle_disabled"])
@pytest.mark.parametrize(
    "change",
    [
        {"url": "https://changed.example.test"},
        {"data": {"timeout": 17}},
        {"disabled": True},
    ],
)
def test_old_definition_refuses_toggle_with_fresh_card(client, check, path, change):
    token = revision(check)
    HealthCheck.objects.filter(pk=check.pk).update(**change)
    response = post(client, check, path, token)
    assert response.status_code == 409
    check.refresh_from_db()
    assert check.disabled is change.get("disabled", False)
    assert response.context["check"].url == check.url
    assert "reload" in response.content.decode().lower()
    assert revision(check) in response.content.decode()


@pytest.mark.parametrize("path", ["healthcheck_update", "healthcheck_toggle_disabled"])
@pytest.mark.parametrize("token", ["", "tampered"])
def test_missing_or_invalid_revision_refuses(client, check, path, token):
    assert post(client, check, path, token).status_code == 409
    check.refresh_from_db()
    assert not check.disabled


@pytest.mark.parametrize("path", ["healthcheck_update", "healthcheck_toggle_disabled"])
def test_change_at_conditional_update_refuses(client, check, path, monkeypatch):
    token = revision(check)
    original = QuerySet.update
    injected = []

    def update(query, **values):
        if query.model is HealthCheck and "disabled" in values and not injected:
            injected.append(True)
            original(HealthCheck.objects.filter(pk=check.pk), data={"timeout": 19})
        return original(query, **values)

    monkeypatch.setattr(QuerySet, "update", update)
    response = post(client, check, path, token)
    assert injected
    assert response.status_code == 409
    check.refresh_from_db()
    assert not check.disabled
    assert check.data == {"timeout": 19}


@pytest.mark.parametrize("path", ["healthcheck_update", "healthcheck_toggle_disabled"])
def test_fresh_toggle_preserves_agent_runtime(client, check, path):
    token = revision(check)
    HealthCheck.objects.filter(pk=check.pk).update(
        status="processing", processing_started_at=777
    )
    response = post(client, check, path, token)
    assert response.status_code == (302 if path == "healthcheck_update" else 200)
    check.refresh_from_db()
    assert check.disabled
    assert check.status == "processing"
    assert check.processing_started_at == 777
    # Replaying the old card cannot toggle a second time.
    assert post(client, check, path, token).status_code == 409


def test_card_sends_revision_without_exposing_configuration(client, check):
    HealthCheck.objects.filter(pk=check.pk).update(
        data={"password": "synthetic-private-value"}
    )
    response = client.get(reverse("nyxboard:dashboard"))
    card = BeautifulSoup(response.content, "html.parser").select_one(
        "[data-check-toggle]"
    )
    check.refresh_from_db()
    assert json.loads(card["hx-vals"]) == {"definition_revision": revision(check)}
    assert "synthetic-private-value" not in response.content.decode()
