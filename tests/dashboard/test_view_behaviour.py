"""Behaviour tests for every NyxBoard view.

These cover the request-method contracts (mutations happen only on POST), 404s
for unknown objects, the HTMX partial switch on ``HX-Request-URL``, CRUD for
services and health checks, and the side effects of the trigger and toggle
endpoints. They run with ``NYXBOARD_REQUIRE_LOGIN`` at its default (off); the
login requirement is covered in ``test_login_required.py``.
"""

import json
from time import time

import pytest
from django.test import Client
from django.urls import reverse

from nyxmon.domain import CheckStatus, ResultStatus
from nyxboard.models import HealthCheck, Result, Service

CARD_PARTIAL = "nyxboard/partials/healthcheck-card.html"
LIST_PARTIAL = "nyxboard/partials/healthcheck.html"

PARTIAL_VIEWS = [
    "nyxboard:healthcheck_update_status",
    "nyxboard:healthcheck_trigger",
    "nyxboard:healthcheck_toggle_disabled",
]


def template_names(response):
    return [t.name for t in response.templates if t.name]


@pytest.fixture
def service():
    return Service.objects.create(name="Web")


@pytest.fixture
def check(service):
    return HealthCheck.objects.create(
        service=service,
        name="Homepage",
        check_type="http",
        url="https://example.com",
        check_interval=300,
        next_check_time=int(time()) + 3600,
    )


def http_check_payload(service, **overrides):
    payload = {
        "name": "Homepage",
        "service": service.id,
        "check_type": "http",
        "url": "https://example.com/health",
        "check_interval": 300,
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# 404s for unknown objects
# --------------------------------------------------------------------------

MISSING_ID = 999_999

NOT_FOUND_CASES = [
    ("get", "nyxboard:service_detail", {"service_id": MISSING_ID}),
    ("get", "nyxboard:service_update", {"service_id": MISSING_ID}),
    ("post", "nyxboard:service_update", {"service_id": MISSING_ID}),
    ("get", "nyxboard:service_delete", {"service_id": MISSING_ID}),
    ("post", "nyxboard:service_delete", {"service_id": MISSING_ID}),
    ("get", "nyxboard:healthcheck_create_for_service", {"service_id": MISSING_ID}),
    ("post", "nyxboard:healthcheck_create_for_service", {"service_id": MISSING_ID}),
    ("get", "nyxboard:healthcheck_detail", {"check_id": MISSING_ID}),
    ("get", "nyxboard:healthcheck_update", {"check_id": MISSING_ID}),
    ("post", "nyxboard:healthcheck_update", {"check_id": MISSING_ID}),
    ("get", "nyxboard:healthcheck_delete", {"check_id": MISSING_ID}),
    ("post", "nyxboard:healthcheck_delete", {"check_id": MISSING_ID}),
    ("get", "nyxboard:healthcheck_update_status", {"check_id": MISSING_ID}),
    ("post", "nyxboard:healthcheck_trigger", {"check_id": MISSING_ID}),
    ("post", "nyxboard:healthcheck_toggle_disabled", {"check_id": MISSING_ID}),
]


@pytest.mark.django_db
@pytest.mark.parametrize("method,name,kwargs", NOT_FOUND_CASES)
def test_unknown_object_returns_404(client, method, name, kwargs):
    response = getattr(client, method)(reverse(name, kwargs=kwargs))
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Dashboard and list pages
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestDashboard:
    def test_due_and_normal_modes_and_last_result(self, client, service):
        now = int(time())
        due = HealthCheck.objects.create(
            service=service, name="due", url="https://a.example", next_check_time=0
        )
        normal = HealthCheck.objects.create(
            service=service,
            name="normal",
            url="https://b.example",
            next_check_time=now + 3600,
        )
        Result.objects.create(health_check=due, status=ResultStatus.ERROR, data={})

        response = client.get(reverse("nyxboard:dashboard"))

        assert response.status_code == 200
        results = json.loads(response.context["check_results_json"])
        assert set(results) == {str(due.id)}
        assert "formattedTime" in results[str(due.id)]
        assert response.context["site_connectivity"] is None
        body = response.content.decode()
        assert f'id="check-{due.id}"' in body
        assert f'id="check-{normal.id}"' in body

    def test_sets_default_theme_in_session(self, client):
        response = client.get(reverse("nyxboard:dashboard"))
        assert response.context["theme"] == "light"
        assert client.session["theme"] == "light"

    def test_keeps_existing_session_theme(self, client):
        client.post(
            reverse("nyxboard:set_theme"),
            data=json.dumps({"theme": "dark"}),
            content_type="application/json",
        )
        response = client.get(reverse("nyxboard:dashboard"))
        assert response.context["theme"] == "dark"

    def test_no_logout_button_while_login_not_required(self, client):
        response = client.get(reverse("nyxboard:dashboard"))
        assert reverse("nyxboard:logout") not in response.content.decode()


@pytest.mark.django_db
def test_healthcheck_list_shows_all_checks(client, check, service):
    other = HealthCheck.objects.create(service=service, name="API", url="https://x")
    response = client.get(reverse("nyxboard:healthcheck_list"))
    assert response.status_code == 200
    assert list(response.context["health_checks"]) == [check, other]


@pytest.mark.django_db
def test_healthcheck_detail_limits_results_newest_first(client, check):
    for _ in range(12):
        Result.objects.create(health_check=check, status=ResultStatus.OK, data={})
    response = client.get(
        reverse("nyxboard:healthcheck_detail", kwargs={"check_id": check.id})
    )
    results = response.context["results"]
    assert len(results) == 10
    ids = [r.id for r in results]
    assert ids == sorted(ids, reverse=True)
    assert response.context["alert_held"] is False
    assert response.context["held_since"] is None


# --------------------------------------------------------------------------
# Service CRUD
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestServiceCrud:
    def test_create_get_renders_empty_form(self, client):
        response = client.get(reverse("nyxboard:service_create"))
        assert response.status_code == 200
        assert response.context["action"] == "Create"
        assert not response.context["form"].is_bound
        assert Service.objects.count() == 0

    def test_create_post_redirects_to_detail(self, client):
        response = client.post(reverse("nyxboard:service_create"), {"name": "Mail"})
        service = Service.objects.get(name="Mail")
        assert response.status_code == 302
        assert response["Location"] == reverse(
            "nyxboard:service_detail", kwargs={"service_id": service.id}
        )

    def test_create_invalid_rerenders_with_errors(self, client):
        response = client.post(reverse("nyxboard:service_create"), {"name": ""})
        assert response.status_code == 200
        assert "name" in response.context["form"].errors
        assert Service.objects.count() == 0

    def test_update_get_prefills_form(self, client, service):
        response = client.get(
            reverse("nyxboard:service_update", kwargs={"service_id": service.id})
        )
        assert response.status_code == 200
        assert response.context["action"] == "Update"
        assert response.context["service"] == service
        assert response.context["form"].instance == service

    def test_update_post_saves_and_redirects(self, client, service):
        url = reverse("nyxboard:service_update", kwargs={"service_id": service.id})
        response = client.post(url, {"name": "Renamed"})
        assert response.status_code == 302
        assert response["Location"] == reverse(
            "nyxboard:service_detail", kwargs={"service_id": service.id}
        )
        service.refresh_from_db()
        assert service.name == "Renamed"

    def test_update_invalid_does_not_save(self, client, service):
        url = reverse("nyxboard:service_update", kwargs={"service_id": service.id})
        response = client.post(url, {"name": ""})
        assert response.status_code == 200
        assert "name" in response.context["form"].errors
        service.refresh_from_db()
        assert service.name == "Web"

    def test_delete_get_only_confirms(self, client, service, check):
        url = reverse("nyxboard:service_delete", kwargs={"service_id": service.id})
        response = client.get(url)
        assert response.status_code == 200
        assert "nyxboard/service_confirm_delete.html" in template_names(response)
        assert Service.objects.filter(id=service.id).exists()

    def test_delete_post_cascades_to_checks(self, client, service, check):
        url = reverse("nyxboard:service_delete", kwargs={"service_id": service.id})
        response = client.post(url)
        assert response.status_code == 302
        assert response["Location"] == reverse("nyxboard:service_list")
        assert not Service.objects.filter(id=service.id).exists()
        assert not HealthCheck.objects.filter(id=check.id).exists()


# --------------------------------------------------------------------------
# Health check CRUD
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestHealthCheckCreate:
    def test_get_defaults_to_http_form(self, client):
        response = client.get(reverse("nyxboard:healthcheck_create"))
        assert response.status_code == 200
        assert response.context["check_type"] == "http"
        assert "nyxboard/healthcheck_form_http.html" in template_names(response)
        assert response.context["service"] is None

    @pytest.mark.parametrize(
        "check_type,template",
        [
            ("json-http", "nyxboard/healthcheck_form_http.html"),
            ("dns", "nyxboard/healthcheck_form_dns.html"),
            ("tcp", "nyxboard/healthcheck_form_tcp.html"),
            ("smtp", "nyxboard/healthcheck_form_smtp.html"),
            ("imap", "nyxboard/healthcheck_form_imap.html"),
            ("json-metrics", "nyxboard/healthcheck_form_json_metrics.html"),
            ("ping", "nyxboard/healthcheck_form.html"),
        ],
    )
    def test_type_query_selects_template(self, client, check_type, template):
        response = client.get(
            reverse("nyxboard:healthcheck_create") + f"?type={check_type}"
        )
        assert response.status_code == 200
        assert template in template_names(response)
        assert response.context["check_type"] == check_type
        assert response.context["form"].initial["check_type"] == check_type

    def test_for_service_preselects_service(self, client, service):
        response = client.get(
            reverse(
                "nyxboard:healthcheck_create_for_service",
                kwargs={"service_id": service.id},
            )
        )
        assert response.status_code == 200
        assert response.context["service"] == service
        assert response.context["form"].initial["service"] == service

    def test_post_creates_and_redirects(self, client, service):
        response = client.post(
            reverse("nyxboard:healthcheck_create"), http_check_payload(service)
        )
        created = HealthCheck.objects.get(name="Homepage")
        assert response.status_code == 302
        assert response["Location"] == reverse(
            "nyxboard:healthcheck_detail", kwargs={"check_id": created.id}
        )
        assert created.service == service
        assert created.check_type == "http"

    def test_post_invalid_rerenders(self, client, service):
        response = client.post(
            reverse("nyxboard:healthcheck_create"),
            http_check_payload(service, url="not a url"),
        )
        assert response.status_code == 200
        assert "url" in response.context["form"].errors
        assert HealthCheck.objects.count() == 0


@pytest.mark.django_db
class TestHealthCheckUpdate:
    def url(self, check):
        return reverse("nyxboard:healthcheck_update", kwargs={"check_id": check.id})

    def test_get_renders_type_specific_form(self, client, check):
        response = client.get(self.url(check))
        assert response.status_code == 200
        assert response.context["action"] == "Update"
        assert response.context["health_check"] == check
        assert "nyxboard/healthcheck_form_http.html" in template_names(response)

    def test_get_unmapped_type_uses_generic_form(self, client, service):
        ping = HealthCheck.objects.create(
            service=service, name="ping", check_type="ping", url="host"
        )
        response = client.get(self.url(ping))
        assert response.status_code == 200
        assert "nyxboard/healthcheck_form.html" in template_names(response)

    def test_post_without_interval_change_keeps_schedule(self, client, check):
        scheduled = check.next_check_time
        response = client.post(
            self.url(check),
            http_check_payload(check.service, name="Renamed"),
        )
        assert response.status_code == 302
        assert response["Location"] == reverse(
            "nyxboard:healthcheck_detail", kwargs={"check_id": check.id}
        )
        check.refresh_from_db()
        assert check.name == "Renamed"
        assert check.next_check_time == scheduled

    def test_post_interval_change_makes_check_due(self, client, check):
        before = int(time())
        response = client.post(
            self.url(check), http_check_payload(check.service, check_interval=60)
        )
        assert response.status_code == 302
        check.refresh_from_db()
        assert check.check_interval == 60
        assert before <= check.next_check_time <= int(time())

    def test_post_invalid_does_not_save(self, client, check):
        response = client.post(
            self.url(check), http_check_payload(check.service, url="nope")
        )
        assert response.status_code == 200
        assert "url" in response.context["form"].errors
        check.refresh_from_db()
        assert check.url == "https://example.com"

    @pytest.mark.parametrize("value,expected", [("1", True), ("0", False)])
    def test_quick_toggle_sets_disabled_and_redirects_to_dashboard(
        self, client, check, value, expected
    ):
        check.disabled = not expected
        check.save()
        response = client.post(
            self.url(check), {"disabled": value, "csrfmiddlewaretoken": "x"}
        )
        assert response.status_code == 302
        assert response["Location"] == reverse("nyxboard:dashboard")
        check.refresh_from_db()
        assert check.disabled is expected

    def test_quick_toggle_returns_to_referer(self, client, check):
        referer = "http://testserver" + reverse("nyxboard:healthcheck_list")
        response = client.post(
            self.url(check),
            {"disabled": "1", "csrfmiddlewaretoken": "x"},
            HTTP_REFERER=referer,
        )
        assert response.status_code == 302
        assert response["Location"] == referer

    def test_quick_toggle_returns_to_same_host_relative_referer(self, client, check):
        response = client.post(
            self.url(check),
            {"disabled": "1", "csrfmiddlewaretoken": "x"},
            HTTP_REFERER="/healthchecks/",
        )
        assert response["Location"] == "/healthchecks/"

    @pytest.mark.parametrize(
        "referer",
        [
            "https://evil.example/phish",
            "http://evil.example/",
            "//evil.example/",
            "javascript:alert(1)",
        ],
    )
    def test_quick_toggle_ignores_foreign_referer(self, client, check, referer):
        response = client.post(
            self.url(check),
            {"disabled": "1", "csrfmiddlewaretoken": "x"},
            HTTP_REFERER=referer,
        )
        assert response.status_code == 302
        assert response["Location"] == reverse("nyxboard:dashboard")
        check.refresh_from_db()
        assert check.disabled is True

    def test_quick_toggle_https_request_rejects_http_referer(self, client, check):
        response = client.post(
            self.url(check),
            {"disabled": "1", "csrfmiddlewaretoken": "x"},
            HTTP_REFERER="http://testserver/healthchecks/",
            secure=True,
        )
        assert response["Location"] == reverse("nyxboard:dashboard")


@pytest.mark.django_db
class TestHealthCheckDelete:
    def test_get_only_confirms(self, client, check):
        response = client.get(
            reverse("nyxboard:healthcheck_delete", kwargs={"check_id": check.id})
        )
        assert response.status_code == 200
        assert "nyxboard/healthcheck_confirm_delete.html" in template_names(response)
        assert HealthCheck.objects.filter(id=check.id).exists()

    def test_post_deletes_check_and_results(self, client, check, service):
        Result.objects.create(health_check=check, status=ResultStatus.OK, data={})
        response = client.post(
            reverse("nyxboard:healthcheck_delete", kwargs={"check_id": check.id})
        )
        assert response.status_code == 302
        assert response["Location"] == reverse(
            "nyxboard:service_detail", kwargs={"service_id": service.id}
        )
        assert not HealthCheck.objects.filter(id=check.id).exists()
        assert not Result.objects.filter(health_check_id=check.id).exists()
        assert Service.objects.filter(id=service.id).exists()


# --------------------------------------------------------------------------
# HTMX endpoints
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestPartialSwitch:
    """``HX-Request-URL`` containing ``healthcheck.html`` selects the list partial."""

    def request(self, client, name, check, **headers):
        url = reverse(name, kwargs={"check_id": check.id})
        if name == "nyxboard:healthcheck_update_status":
            return client.get(url, headers=headers)
        return client.post(url, headers=headers)

    @pytest.mark.parametrize("name", PARTIAL_VIEWS)
    def test_card_partial_by_default(self, client, check, name):
        response = self.request(client, name, check, HX_Request="true")
        assert response.status_code == 200
        names = template_names(response)
        assert CARD_PARTIAL in names
        assert LIST_PARTIAL not in names

    @pytest.mark.parametrize("name", PARTIAL_VIEWS)
    def test_list_partial_when_requested(self, client, check, name):
        response = self.request(
            client,
            name,
            check,
            HX_Request="true",
            HX_Request_URL="/static/partials/healthcheck.html",
        )
        assert response.status_code == 200
        names = template_names(response)
        assert LIST_PARTIAL in names
        assert CARD_PARTIAL not in names

    @pytest.mark.parametrize("name", PARTIAL_VIEWS)
    def test_unrelated_request_url_keeps_card(self, client, check, name):
        response = self.request(
            client, name, check, HX_Request="true", HX_Request_URL="/dashboard/"
        )
        assert CARD_PARTIAL in template_names(response)


@pytest.mark.django_db
class TestUpdateStatus:
    def get(self, client, check):
        return client.get(
            reverse("nyxboard:healthcheck_update_status", kwargs={"check_id": check.id})
        )

    def test_normal_when_next_check_in_future(self, client, check):
        response = self.get(client, check)
        assert response.context["check_mode"] == "normal"
        assert response.context["last_result"] is None

    def test_due_when_next_check_passed(self, client, check):
        check.next_check_time = 0
        check.save()
        assert self.get(client, check).context["check_mode"] == "due"

    def test_due_while_processing(self, client, check):
        check.status = CheckStatus.PROCESSING
        check.save()
        assert self.get(client, check).context["check_mode"] == "due"

    def test_last_result_is_newest(self, client, check):
        Result.objects.create(health_check=check, status=ResultStatus.OK, data={})
        newest = Result.objects.create(
            health_check=check, status=ResultStatus.ERROR, data={}
        )
        assert self.get(client, check).context["last_result"] == newest

    def test_does_not_mutate(self, client, check):
        scheduled = check.next_check_time
        self.get(client, check)
        check.refresh_from_db()
        assert check.next_check_time == scheduled
        assert check.disabled is False


@pytest.mark.django_db
class TestTrigger:
    def url(self, check):
        return reverse("nyxboard:healthcheck_trigger", kwargs={"check_id": check.id})

    def test_get_redirects_without_side_effect(self, client, check):
        scheduled = check.next_check_time
        response = client.get(self.url(check))
        assert response.status_code == 302
        assert response["Location"] == reverse("nyxboard:dashboard")
        check.refresh_from_db()
        assert check.next_check_time == scheduled

    def test_post_makes_check_due_now(self, client, check):
        Result.objects.create(health_check=check, status=ResultStatus.OK, data={})
        before = int(time())
        response = client.post(self.url(check))
        assert response.status_code == 200
        assert response.context["check_mode"] == "due"
        assert response.context["last_result"] is not None
        check.refresh_from_db()
        assert before <= check.next_check_time <= int(time())
        assert check.disabled is False


@pytest.mark.django_db
class TestToggleDisabled:
    def url(self, check):
        return reverse(
            "nyxboard:healthcheck_toggle_disabled", kwargs={"check_id": check.id}
        )

    def test_get_redirects_without_side_effect(self, client, check):
        response = client.get(self.url(check))
        assert response.status_code == 302
        assert response["Location"] == reverse("nyxboard:dashboard")
        check.refresh_from_db()
        assert check.disabled is False

    def test_disabling_resets_schedule_and_shows_normal(self, client, check):
        before = int(time())
        response = client.post(self.url(check))
        assert response.status_code == 200
        assert response.context["check_mode"] == "normal"
        check.refresh_from_db()
        assert check.disabled is True
        assert before <= check.next_check_time <= int(time())

    def test_enabling_overdue_check_shows_due(self, client, check):
        check.disabled = True
        check.next_check_time = 0
        check.save()
        response = client.post(self.url(check))
        assert response.context["check_mode"] == "due"
        check.refresh_from_db()
        assert check.disabled is False
        assert check.next_check_time == 0

    def test_enabling_future_check_keeps_schedule(self, client, check):
        check.disabled = True
        check.save()
        scheduled = check.next_check_time
        response = client.post(self.url(check))
        assert response.context["check_mode"] == "normal"
        check.refresh_from_db()
        assert check.disabled is False
        assert check.next_check_time == scheduled


# --------------------------------------------------------------------------
# set_theme
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestSetTheme:
    def test_get_not_allowed(self, client):
        assert client.get(reverse("nyxboard:set_theme")).status_code == 405

    def test_post_stores_theme(self, client):
        response = client.post(
            reverse("nyxboard:set_theme"),
            data=json.dumps({"theme": "dark"}),
            content_type="application/json",
        )
        assert response.status_code == 200
        assert response.json() == {"status": "success", "theme": "dark"}
        assert client.session["theme"] == "dark"

    def test_missing_theme_defaults_to_light(self, client):
        response = client.post(
            reverse("nyxboard:set_theme"), data="{}", content_type="application/json"
        )
        assert response.json()["theme"] == "light"

    def test_invalid_json_is_400(self, client):
        response = client.post(
            reverse("nyxboard:set_theme"),
            data="not json",
            content_type="application/json",
        )
        assert response.status_code == 400
        assert response.json()["status"] == "error"


# --------------------------------------------------------------------------
# CSRF protects every mutation
# --------------------------------------------------------------------------

CSRF_MUTATIONS = [
    ("nyxboard:service_create", None),
    ("nyxboard:service_update", "service"),
    ("nyxboard:service_delete", "service"),
    ("nyxboard:healthcheck_create", None),
    ("nyxboard:healthcheck_update", "check"),
    ("nyxboard:healthcheck_delete", "check"),
    ("nyxboard:healthcheck_trigger", "check"),
    ("nyxboard:healthcheck_toggle_disabled", "check"),
    ("nyxboard:set_theme", None),
]


@pytest.mark.django_db
@pytest.mark.parametrize("name,target", CSRF_MUTATIONS)
def test_mutations_require_csrf_token(service, check, name, target):
    kwargs = {}
    if target == "service":
        kwargs = {"service_id": service.id}
    elif target == "check":
        kwargs = {"check_id": check.id}
    csrf_client = Client(enforce_csrf_checks=True)
    scheduled = check.next_check_time

    response = csrf_client.post(reverse(name, kwargs=kwargs), {"name": "x"})

    assert response.status_code == 403
    assert Service.objects.filter(id=service.id, name="Web").exists()
    check.refresh_from_db()
    assert check.next_check_time == scheduled
    assert check.disabled is False
