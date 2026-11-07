"""Synthetic interleavings through the real edit forms and view."""

import pytest
from django.urls import reverse
from nyxboard.forms import GenericHealthCheckForm
from nyxboard.models import HealthCheck
from nyxboard.views import FORM_CLASSES


TYPES = [*FORM_CLASSES, "custom"]


def payload(form):
    """Submit the displayed defaults, as a browser would."""
    return {
        name: field.value()
        for name, field in ((name, form[name]) for name in form.fields)
        if field.value() is not None and field.value() is not False
    }


@pytest.fixture
def check(service_factory, healthcheck_factory):
    return healthcheck_factory(service_factory(), name="Original", next_check_time=9000)


def configure(check, kind):
    check.check_type = kind
    check.data = {
        "host": "mail.example.com",
        "port": 443,
        "tls": "none",
        "username": "synthetic",
        "password": "fixture",
        "from_addr": "monitor@example.com",
        "to_addr": "test@example.com",
        "subject_prefix": "[test]",
        "search_subject": "[test]",
        "expected_ips": ["192.0.2.1"],
        "checks": [],
    }
    check.save()


@pytest.mark.django_db
@pytest.mark.parametrize("kind", TYPES)
@pytest.mark.parametrize(
    "runtime",
    [
        dict(status="processing", processing_started_at=111, next_check_time=9000),
        dict(status="idle", processing_started_at=0, next_check_time=12000),
    ],
)
@pytest.mark.parametrize("interval", [300, 60])
def test_every_form_preserves_concurrent_runtime(check, kind, runtime, interval):
    configure(check, kind)
    cls = FORM_CLASSES.get(kind, GenericHealthCheckForm)
    data = payload(cls(instance=check))
    data.update(name="Edited", check_interval=interval)
    if kind == "dns":
        data["url"] = "example.com"
    form = cls(data, instance=check)
    assert form.is_valid(), form.errors
    HealthCheck.objects.filter(pk=check.pk).update(**runtime)
    saved = form.save()
    check.refresh_from_db()
    assert check.name == "Edited"
    assert check.check_interval == interval
    assert {key: getattr(check, key) for key in runtime} == runtime
    assert {key: getattr(saved, key) for key in runtime} == runtime


def edit_url(check):
    return reverse("nyxboard:healthcheck_update", args=[check.pk])


@pytest.mark.django_db
@pytest.mark.parametrize("kind", TYPES)
def test_rendered_revision_blocks_stale_tabs(client, check, kind):
    configure(check, kind)
    url = edit_url(check)
    first = client.get(url)
    form = first.context["form"]
    assert 'name="definition_revision"' in first.content.decode()
    stale = payload(form)
    if kind == "dns":
        stale["url"] = "example.com"
    fresh = dict(stale, check_interval=60)
    assert client.post(url, fresh).status_code == 302
    response = client.post(url, dict(stale, name="Stale operator"))
    assert response.status_code == 200
    assert "reload" in response.content.decode().lower()
    assert response.context["form"]["name"].value() == "Stale operator"
    check.refresh_from_db()
    assert check.check_interval == 60
    assert check.name == "Original"


@pytest.mark.django_db
@pytest.mark.parametrize("revision", [None, "tampered"])
def test_missing_or_invalid_revision_is_not_saved(client, check, revision):
    data = payload(client.get(edit_url(check)).context["form"])
    data.pop("definition_revision", None)
    if revision:
        data["definition_revision"] = revision
    response = client.post(edit_url(check), dict(data, name="Rejected"))
    assert response.status_code == 200
    assert response.context["form"].errors
    check.refresh_from_db()
    assert check.name == "Original"


@pytest.mark.django_db
@pytest.mark.parametrize("operation", ["edit", "delete"])
def test_change_after_validation_is_conflict(client, check, monkeypatch, operation):
    from nyxboard.forms import HttpHealthCheckForm

    data = payload(client.get(edit_url(check)).context["form"])
    original = HttpHealthCheckForm.save
    injected = []

    def save(form, *args, **kwargs):
        injected.append(True)
        row = HealthCheck.objects.filter(pk=check.pk)
        if operation == "delete":
            row.delete()
        else:
            row.update(data={"owner": "new"})
        return original(form, *args, **kwargs)

    monkeypatch.setattr(HttpHealthCheckForm, "save", save)
    response = client.post(edit_url(check), dict(data, name="Submitted"))
    assert injected
    assert response.status_code == 200
    assert response.context["form"]["name"].value() == "Submitted"
    assert "reload" in response.content.decode().lower()
    if operation == "delete":
        assert not HealthCheck.objects.filter(pk=check.pk).exists()
    else:
        check.refresh_from_db()
        assert check.data == {"owner": "new"}
        assert check.name == "Original"


@pytest.mark.django_db
def test_deleted_between_get_and_post_retains_values(client, check):
    url = edit_url(check)
    data = payload(client.get(url).context["form"])
    check.delete()
    response = client.post(url, dict(data, name="Unsaved work"))
    assert response.status_code == 200
    assert response.context["form"]["name"].value() == "Unsaved work"
    assert "reload" in response.content.decode().lower()
    assert HealthCheck.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("interval", [300, 60])
def test_edit_post_preserves_claim_during_save(client, check, monkeypatch, interval):
    from nyxboard.forms import HttpHealthCheckForm

    data = payload(client.get(edit_url(check)).context["form"])
    original = HttpHealthCheckForm.save
    runtime = dict(
        status="processing", processing_started_at=777, next_check_time=14000
    )

    def save(form, *args, **kwargs):
        HealthCheck.objects.filter(pk=check.pk).update(**runtime)
        return original(form, *args, **kwargs)

    monkeypatch.setattr(HttpHealthCheckForm, "save", save)
    response = client.post(
        edit_url(check), dict(data, name="Edited", check_interval=interval)
    )
    assert response.status_code == 302
    check.refresh_from_db()
    assert check.name == "Edited"
    assert {key: getattr(check, key) for key in runtime} == runtime


@pytest.mark.django_db
@pytest.mark.parametrize(
    "changes",
    [
        {"url": "https://example.com/changed"},
        {"data": {"auth": {"password": "fixture"}}},
        {"disabled": True},
        {"check_type": "json-http"},
        {"check_interval": 60},
    ],
)
def test_definition_fingerprint_covers_probe_and_enablement(client, check, changes):
    data = payload(client.get(edit_url(check)).context["form"])
    HealthCheck.objects.filter(pk=check.pk).update(**changes)
    response = client.post(edit_url(check), dict(data, name="Stale"))
    assert response.status_code == 200
    assert response.context["form"].errors
    check.refresh_from_db()
    assert check.name == "Original"
    assert {key: getattr(check, key) for key in changes} == changes


@pytest.mark.django_db
def test_compact_json_is_editable_and_revision_stays_hidden(client, check):
    from bs4 import BeautifulSoup
    from django.db import connection

    check.check_type = "custom"
    check.save()
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE health_check SET data = %s WHERE id = %s",
            ['{"label":"café"}', check.pk],
        )
    response = client.get(edit_url(check))
    soup = BeautifulSoup(response.content, "html.parser")
    assert soup.select_one('input[name="definition_revision"]')["type"] == "hidden"
    assert not soup.select('label[for="id_definition_revision"]')
    data = payload(response.context["form"])
    assert client.post(edit_url(check), dict(data, name="Edited")).status_code == 302
    check.refresh_from_db()
    assert check.name == "Edited"
    assert check.data == {"label": "café"}


@pytest.mark.django_db
@pytest.mark.parametrize("replacement", [True, 1.0])
def test_json_type_change_after_load_is_a_conflict(
    client, check, monkeypatch, replacement
):
    from nyxboard.forms import HttpHealthCheckForm

    HealthCheck.objects.filter(pk=check.pk).update(data={"value": 1})
    data = payload(client.get(edit_url(check)).context["form"])
    original = HttpHealthCheckForm.save

    def save(form, *args, **kwargs):
        HealthCheck.objects.filter(pk=check.pk).update(data={"value": replacement})
        return original(form, *args, **kwargs)

    monkeypatch.setattr(HttpHealthCheckForm, "save", save)
    response = client.post(edit_url(check), dict(data, name="Stale"))
    assert response.status_code == 200
    assert "reload" in response.content.decode().lower()
    check.refresh_from_db()
    assert check.name == "Original"
    assert type(check.data["value"]) is type(replacement)
