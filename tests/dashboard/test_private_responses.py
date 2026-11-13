"""Private pages and rejected fragments must never enter browser caches."""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_login_read_logout_never_stores_private_responses(
    client, settings, django_user_model, service_factory, healthcheck_factory
):
    settings.NYXBOARD_REQUIRE_LOGIN = True
    user = django_user_model.objects.create_user("synthetic", password="synthetic")
    check = healthcheck_factory(service_factory())
    routes = [
        ("dashboard", {}),
        ("service_list", {}),
        ("service_detail", {"service_id": check.service_id}),
        ("healthcheck_list", {}),
        ("healthcheck_detail", {"check_id": check.pk}),
        ("healthcheck_update", {"check_id": check.pk}),
        ("healthcheck_delete", {"check_id": check.pk}),
        ("healthcheck_update_status", {"check_id": check.pk}),
        ("healthcheck_update_status", {"check_id": 99999}),
        ("set_theme", {}),
    ]
    client.force_login(user)
    for name, kwargs in routes:
        response = client.get(
            reverse(f"nyxboard:{name}", kwargs=kwargs), HTTP_HX_REQUEST="true"
        )
        assert response.status_code in (200, 404, 405)
        assert {"private", "no-store", "no-cache", "max-age=0"} <= set(
            response["Cache-Control"].split(", ")
        )
        assert "Expires" in response
    client.post(reverse("nyxboard:logout"))
    for name, kwargs in routes:
        response = client.get(
            reverse(f"nyxboard:{name}", kwargs=kwargs), HTTP_HX_REQUEST="true"
        )
        assert response.status_code == 403
        assert "no-store" in response["Cache-Control"]
    response = client.get(reverse("nyxboard:dashboard"))
    assert response.status_code == 302
    assert "no-store" in response["Cache-Control"]


def test_proxy_protected_mode_is_also_private(client):
    response = client.get(reverse("nyxboard:dashboard"))
    assert "no-store" in response["Cache-Control"]


def test_csrf_rejection_is_private(service_factory, healthcheck_factory):
    from django.test import Client

    check = healthcheck_factory(service_factory())
    response = Client(enforce_csrf_checks=True).post(
        reverse("nyxboard:healthcheck_toggle_disabled", args=[check.pk])
    )
    assert response.status_code == 403
    assert "no-store" in response["Cache-Control"]
