"""Navigation survives template inheritance and invalid bound forms."""

import pytest
from bs4 import BeautifulSoup
from django.urls import reverse
from nyxboard.models import HealthCheck, Service

pytestmark = pytest.mark.django_db


def assert_navigation(response, title, parent):
    assert response.status_code == 200
    soup = BeautifulSoup(response.content, "html.parser")
    assert title in soup.select_one(".form-header h2").get_text()
    assert soup.select_one("a.back-link")["href"].strip() == parent
    cancel = next(
        a for a in soup.select(".form-actions a") if a.get_text(strip=True) == "Cancel"
    )
    assert cancel["href"].strip() == parent


@pytest.mark.parametrize("method", ["get", "post"])
def test_service_form_navigation(client, method):
    service = Service.objects.create(name="Synthetic service")
    for route, kwargs, parent in [
        ("service_create", {}, reverse("nyxboard:service_list")),
        (
            "service_update",
            {"service_id": service.id},
            reverse("nyxboard:service_detail", args=[service.id]),
        ),
    ]:
        response = getattr(client, method)(
            reverse(f"nyxboard:{route}", kwargs=kwargs),
            data={} if method == "post" else None,
        )
        assert_navigation(response, "Service", parent)
        if method == "post":
            assert response.context["form"].errors


@pytest.mark.parametrize(
    "check_type",
    [
        "http",
        "json-http",
        "dns",
        "tcp",
        "ping",
        "smtp",
        "imap",
        "json-metrics",
        "legacy",
    ],
)
@pytest.mark.parametrize("method", ["get", "post"])
def test_check_form_navigation(client, check_type, method):
    service = Service.objects.create(name="Synthetic service")
    check = HealthCheck.objects.create(
        service=service,
        name="Synthetic check",
        check_type=check_type,
        url="example.test",
    )
    for url, parent in [
        (
            reverse("nyxboard:healthcheck_create") + f"?type={check_type}",
            reverse("nyxboard:healthcheck_list"),
        ),
        (
            reverse("nyxboard:healthcheck_create_for_service", args=[service.id])
            + f"?type={check_type}",
            reverse("nyxboard:service_detail", args=[service.id]),
        ),
        (
            reverse("nyxboard:healthcheck_update", args=[check.id]),
            reverse("nyxboard:healthcheck_detail", args=[check.id]),
        ),
    ]:
        response = getattr(client, method)(url, data={} if method == "post" else None)
        assert_navigation(response, "Check", parent)
        if method == "post":
            assert response.context["form"].errors
