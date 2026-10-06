"""Tests for the opt-in ``NYXBOARD_REQUIRE_LOGIN`` setting."""

import json
import os
import subprocess
import sys
from pathlib import Path
from time import time
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.urls import URLPattern, get_resolver, reverse

from nyxboard import urls as nyxboard_urls
from nyxboard.auth import LOGIN_GUARD_ATTR
from nyxboard.models import HealthCheck, Service

LOGIN_URL = "/accounts/login/"
AUTH_VIEW_NAMES = {"login", "logout"}



@pytest.fixture
def service():
    return Service.objects.create(name="Web")


@pytest.fixture
def check(service):
    return HealthCheck.objects.create(
        service=service,
        name="Homepage",
        url="https://example.com",
        next_check_time=int(time()) + 3600,
    )


@pytest.fixture
def user():
    return get_user_model().objects.create_user("operator", password="pw-for-tests")


def page_requests(service, check):
    """Every NyxBoard page as (method, url)."""
    s = {"service_id": service.id}
    c = {"check_id": check.id}
    return [
        ("get", reverse("nyxboard:dashboard")),
        ("get", reverse("nyxboard:service_list")),
        ("get", reverse("nyxboard:service_create")),
        ("post", reverse("nyxboard:service_create")),
        ("get", reverse("nyxboard:service_detail", kwargs=s)),
        ("get", reverse("nyxboard:service_update", kwargs=s)),
        ("post", reverse("nyxboard:service_update", kwargs=s)),
        ("get", reverse("nyxboard:service_delete", kwargs=s)),
        ("post", reverse("nyxboard:service_delete", kwargs=s)),
        ("get", reverse("nyxboard:healthcheck_list")),
        ("get", reverse("nyxboard:healthcheck_create")),
        ("post", reverse("nyxboard:healthcheck_create")),
        ("get", reverse("nyxboard:healthcheck_create_for_service", kwargs=s)),
        ("get", reverse("nyxboard:healthcheck_detail", kwargs=c)),
        ("get", reverse("nyxboard:healthcheck_update", kwargs=c)),
        ("post", reverse("nyxboard:healthcheck_update", kwargs=c)),
        ("get", reverse("nyxboard:healthcheck_delete", kwargs=c)),
        ("post", reverse("nyxboard:healthcheck_delete", kwargs=c)),
        ("get", reverse("nyxboard:healthcheck_update_status", kwargs=c)),
        ("post", reverse("nyxboard:healthcheck_trigger", kwargs=c)),
        ("post", reverse("nyxboard:healthcheck_toggle_disabled", kwargs=c)),
    ]


def assert_unchanged(service, check, scheduled):
    assert Service.objects.filter(id=service.id, name="Web").exists()
    check.refresh_from_db()
    assert check.disabled is False
    assert check.next_check_time == scheduled


def test_every_nyxboard_view_is_guarded():
    """A new NyxBoard view cannot silently bypass the login requirement."""
    patterns = [p for p in nyxboard_urls.urlpatterns if isinstance(p, URLPattern)]
    assert len(patterns) == len(nyxboard_urls.urlpatterns)
    unguarded = [
        p.name
        for p in patterns
        if p.name not in AUTH_VIEW_NAMES
        and not getattr(p.callback, LOGIN_GUARD_ATTR, False)
    ]
    assert unguarded == []


def test_login_url_resolves_to_nyxboard_login():
    assert reverse("nyxboard:login") == LOGIN_URL
    match = get_resolver().resolve(LOGIN_URL)
    assert match.view_name == "nyxboard:login"


def test_setting_defaults_to_off_in_base_settings():
    """``config.settings.base`` leaves the requirement off when the env is unset."""
    env = {k: v for k, v in os.environ.items() if k != "NYXBOARD_REQUIRE_LOGIN"}
    django_dir = Path(__file__).resolve().parents[2] / "src" / "django"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(django_dir), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    script = "import config.settings.base as b; print(b.NYXBOARD_REQUIRE_LOGIN)"
    out = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        cwd=django_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "False"


@pytest.mark.django_db
class TestLoginNotRequired:
    """Default mode: anonymous access behaves exactly as before."""

    def test_anonymous_pages_render(self, client, service, check):
        for method, url in page_requests(service, check):
            if method != "get":
                continue
            response = client.get(url)
            assert response.status_code == 200, url

    def test_anonymous_htmx_mutation_allowed(self, client, check):
        response = client.post(
            reverse("nyxboard:healthcheck_trigger", kwargs={"check_id": check.id}),
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200

    def test_anonymous_set_theme_allowed(self, client):
        response = client.post(
            reverse("nyxboard:set_theme"),
            data=json.dumps({"theme": "dark"}),
            content_type="application/json",
        )
        assert response.status_code == 200


@pytest.mark.django_db
class TestLoginRequired:
    @pytest.fixture(autouse=True)
    def require_login(self, settings):
        settings.NYXBOARD_REQUIRE_LOGIN = True

    def test_anonymous_pages_redirect_to_login(self, client, service, check):
        scheduled = check.next_check_time
        for method, url in page_requests(service, check):
            response = getattr(client, method)(url)
            assert response.status_code == 302, (method, url)
            location = urlsplit(response["Location"])
            assert location.path == LOGIN_URL, (method, url)
            assert parse_qs(location.query)["next"] == [url]
        assert_unchanged(service, check, scheduled)

    def test_redirect_preserves_query_string(self, client):
        url = reverse("nyxboard:healthcheck_create") + "?type=dns"
        response = client.get(url)
        assert parse_qs(urlsplit(response["Location"]).query)["next"] == [url]

    def test_anonymous_htmx_requests_get_403_with_hx_redirect(
        self, client, service, check
    ):
        scheduled = check.next_check_time
        page = "http://testserver" + reverse(
            "nyxboard:service_detail", kwargs={"service_id": service.id}
        )
        for method, url in page_requests(service, check):
            response = getattr(client, method)(
                url, headers={"HX-Request": "true", "HX-Current-URL": page}
            )
            assert response.status_code == 403, (method, url)
            redirect = urlsplit(response["HX-Redirect"])
            assert redirect.path == LOGIN_URL
            assert parse_qs(redirect.query)["next"] == [urlsplit(page).path]
            assert "Location" not in response
        assert_unchanged(service, check, scheduled)

    def test_htmx_next_keeps_query_of_current_page(self, client, check):
        response = client.get(
            reverse("nyxboard:healthcheck_update_status", kwargs={"check_id": check.id}),
            headers={
                "HX-Request": "true",
                "HX-Current-URL": "http://testserver/healthchecks/?x=1",
            },
        )
        redirect = urlsplit(response["HX-Redirect"])
        assert parse_qs(redirect.query)["next"] == ["/healthchecks/?x=1"]

    @pytest.mark.parametrize(
        "current",
        ["", "https://evil.example/phish", "//evil.example/", "javascript:alert(1)"],
    )
    def test_htmx_next_ignores_foreign_current_url(self, client, check, current):
        headers = {"HX-Request": "true"}
        if current:
            headers["HX-Current-URL"] = current
        response = client.get(
            reverse("nyxboard:healthcheck_update_status", kwargs={"check_id": check.id}),
            headers=headers,
        )
        assert response.status_code == 403
        redirect = urlsplit(response["HX-Redirect"])
        assert redirect.path == LOGIN_URL
        assert parse_qs(redirect.query)["next"] == [reverse("nyxboard:dashboard")]

    def test_anonymous_set_theme_gets_json_403(self, client):
        response = client.post(
            reverse("nyxboard:set_theme"),
            data=json.dumps({"theme": "dark"}),
            content_type="application/json",
        )
        assert response.status_code == 403
        assert response.json() == {
            "status": "error",
            "message": "Authentication required",
        }
        assert "theme" not in client.session

    def test_authenticated_user_has_full_access(self, client, user, service, check):
        client.force_login(user)
        for method, url in page_requests(service, check):
            if method != "get":
                continue
            response = client.get(url)
            assert response.status_code == 200, url
        response = client.post(
            reverse("nyxboard:healthcheck_toggle_disabled", kwargs={"check_id": check.id}),
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        check.refresh_from_db()
        assert check.disabled is True
        response = client.post(
            reverse("nyxboard:set_theme"),
            data=json.dumps({"theme": "dark"}),
            content_type="application/json",
        )
        assert response.status_code == 200

    def test_login_page_is_reachable_anonymously(self, client):
        response = client.get(LOGIN_URL + "?next=/healthchecks/")
        assert response.status_code == 200
        body = response.content.decode()
        assert 'name="username"' in body
        assert 'value="/healthchecks/"' in body

    def test_login_redirects_to_next(self, client, user):
        response = client.post(
            LOGIN_URL,
            {"username": "operator", "password": "pw-for-tests", "next": "/healthchecks/"},
        )
        assert response.status_code == 302
        assert response["Location"] == "/healthchecks/"
        assert client.get("/healthchecks/").status_code == 200

    def test_login_without_next_lands_on_dashboard(self, client, user):
        response = client.post(
            LOGIN_URL, {"username": "operator", "password": "pw-for-tests"}
        )
        assert response["Location"] == reverse("nyxboard:dashboard")

    def test_login_rejects_foreign_next(self, client, user):
        response = client.post(
            LOGIN_URL,
            {
                "username": "operator",
                "password": "pw-for-tests",
                "next": "https://evil.example/",
            },
        )
        assert response["Location"] == reverse("nyxboard:dashboard")

    def test_wrong_password_stays_on_login(self, client, user):
        response = client.post(LOGIN_URL, {"username": "operator", "password": "no"})
        assert response.status_code == 200
        assert response.context["form"].errors
        assert client.get(reverse("nyxboard:dashboard")).status_code == 302

    def test_logout_button_and_logout(self, client, user):
        client.force_login(user)
        body = client.get(reverse("nyxboard:dashboard")).content.decode()
        assert reverse("nyxboard:logout") in body
        response = client.post(reverse("nyxboard:logout"))
        assert response.status_code == 302
        assert response["Location"] == LOGIN_URL
        assert client.get(reverse("nyxboard:dashboard")).status_code == 302
