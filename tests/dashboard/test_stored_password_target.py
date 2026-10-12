"""A stored check password is only reused for the server it was saved for."""

import pytest

from nyxboard.forms import (
    ImapHealthCheckForm,
    JsonMetricsHealthCheckForm,
    SmtpHealthCheckForm,
)
from nyxboard.models import HealthCheck, Service
from nyxmon.domain import CheckType

pytestmark = pytest.mark.django_db


@pytest.fixture
def service():
    return Service.objects.create(name="Mail")


SMTP_DATA = {
    "host": "mail.example.com",
    "port": 587,
    "tls": "starttls",
    "username": "user@example.com",
    "password": "original_secret",
    "from_addr": "monitor@example.com",
    "to_addr": "test@example.com",
    "subject_prefix": "[nyxmon]",
    "timeout": 30.0,
    "retries": 2,
    "retry_delay": 5.0,
}
IMAP_DATA = {
    "host": "mail.example.com",
    "port": 993,
    "tls_mode": "implicit",
    "username": "user@example.com",
    "password": "original_secret",
    "folder": "INBOX",
    "search_subject": "[nyxmon]",
    "max_age_minutes": 30,
    "delete_after_check": True,
    "timeout": 30.0,
    "retries": 2,
    "retry_delay": 10.0,
}
JSON_DATA = {
    "url": "https://metrics.example.com/health",
    "auth": {"username": "nyxmon", "password": "original_secret"},
    "timeout": 10.0,
    "retries": 1,
    "retry_delay": 2.0,
    "checks": [],
}


def smtp_form(service, **changes):
    check = HealthCheck.objects.create(
        name="SMTP",
        service=service,
        check_type=CheckType.SMTP,
        url="mail.example.com",
        check_interval=300,
        data=dict(SMTP_DATA),
    )
    post = {
        "name": "SMTP",
        "service": service.id,
        "check_type": CheckType.SMTP,
        "check_interval": 300,
        "host": "mail.example.com",
        "port": 587,
        "tls_mode": "starttls",
        "verify": "true",
        "username": "user@example.com",
        "password": "",
        "from_addr": "monitor@example.com",
        "to_addr": "test@example.com",
        "subject_prefix": "[nyxmon]",
        "timeout": 30.0,
        "retries": 2,
        "retry_delay": 5.0,
    }
    post.update(changes)
    return SmtpHealthCheckForm(data=post, instance=check)


def imap_form(service, **changes):
    check = HealthCheck.objects.create(
        name="IMAP",
        service=service,
        check_type=CheckType.IMAP,
        url="mail.example.com",
        check_interval=300,
        data=dict(IMAP_DATA),
    )
    post = {
        "name": "IMAP",
        "service": service.id,
        "check_type": CheckType.IMAP,
        "check_interval": 300,
        "host": "mail.example.com",
        "port": 993,
        "tls_mode": "implicit",
        "verify": "true",
        "username": "user@example.com",
        "password": "",
        "folder": "INBOX",
        "search_subject": "[nyxmon]",
        "max_age_minutes": 30,
        "delete_after_check": True,
        "timeout": 30.0,
        "retries": 2,
        "retry_delay": 10.0,
    }
    post.update(changes)
    return ImapHealthCheckForm(data=post, instance=check)


def json_form(service, **changes):
    check = HealthCheck.objects.create(
        name="Metrics",
        service=service,
        check_type=CheckType.JSON_METRICS,
        url=JSON_DATA["url"],
        check_interval=300,
        data={**JSON_DATA, "auth": dict(JSON_DATA["auth"])},
    )
    post = {
        "name": "Metrics",
        "service": service.id,
        "check_type": CheckType.JSON_METRICS,
        "check_interval": 300,
        "url": JSON_DATA["url"],
        "auth_username": "nyxmon",
        "auth_password": "",
        "timeout": 10.0,
        "retries": 1,
        "retry_delay": 2.0,
        "checks_json": "[]",
    }
    post.update(changes)
    return JsonMetricsHealthCheckForm(data=post, instance=check)


MAIL_TARGET_CHANGES = [
    {"host": "collector.example.net"},
    {"port": 2525},
    {"tls_mode": "none"},
    {"verify": "false"},
]


@pytest.mark.parametrize("changes", MAIL_TARGET_CHANGES)
def test_smtp_blank_password_is_rejected_when_target_changes(service, changes):
    form = smtp_form(service, **changes)
    assert not form.is_valid()
    assert "Re-enter the password" in form.errors["password"][0]


@pytest.mark.parametrize(
    "changes",
    [{"host": "collector.example.net"}, {"port": 1143}, {"tls_mode": "starttls"}, {"verify": "false"}],
)
def test_imap_blank_password_is_rejected_when_target_changes(service, changes):
    form = imap_form(service, **changes)
    assert not form.is_valid()
    assert "Re-enter the password" in form.errors["password"][0]


@pytest.mark.parametrize("build", [smtp_form, imap_form])
def test_mail_stored_password_is_kept_for_same_target(service, build):
    form = build(service, host="MAIL.example.com", name="Renamed", timeout=12.0)
    assert form.is_valid(), form.errors
    assert form.save().data["password"] == "original_secret"


@pytest.mark.parametrize("build", [smtp_form, imap_form])
def test_mail_new_password_allows_target_change(service, build):
    form = build(service, host="new-mail.example.com", password="new_secret")
    assert form.is_valid(), form.errors
    saved = form.save().data
    assert saved["host"] == "new-mail.example.com"
    assert saved["password"] == "new_secret"


def test_mail_verification_can_be_switched_on_without_password(service):
    check = HealthCheck.objects.create(
        name="IMAP unverified",
        service=service,
        check_type=CheckType.IMAP,
        url="mail.example.com",
        check_interval=300,
        data={**IMAP_DATA, "verify": False},
    )
    post = imap_form(service).data.copy()
    form = ImapHealthCheckForm(data=post, instance=check)
    assert form.is_valid(), form.errors
    saved = form.save().data
    assert saved["verify"] is True
    assert saved["password"] == "original_secret"


def test_smtp_stored_password_is_dropped_with_the_username(service):
    """Otherwise a later edit could attach it to a changed host."""
    form = smtp_form(service, host="collector.example.net", username="")
    assert form.is_valid(), form.errors
    assert "password" not in form.save().data


@pytest.mark.parametrize(
    "url",
    [
        "https://collector.example.net/health",
        "http://metrics.example.com/health",
        "https://metrics.example.com:8443/health",
        "https://metrics.example.com.collector.example.net/health",
        "https://metrics.example.com@collector.example.net/health",
    ],
)
def test_json_metrics_blank_password_is_rejected_for_other_origin(service, url):
    form = json_form(service, url=url)
    assert not form.is_valid()
    assert "Re-enter the password" in form.errors["auth_password"][0]


@pytest.mark.parametrize(
    "url",
    ["https://metrics.example.com/other/path?x=1", "https://METRICS.example.com:443/health"],
)
def test_json_metrics_stored_password_is_kept_for_same_origin(service, url):
    form = json_form(service, url=url)
    assert form.is_valid(), form.errors
    assert form.save().data["auth"] == {"username": "nyxmon", "password": "original_secret"}


def test_json_metrics_new_password_allows_other_origin(service):
    form = json_form(
        service, url="https://collector.example.net/health", auth_password="new_secret"
    )
    assert form.is_valid(), form.errors
    assert form.save().data["auth"]["password"] == "new_secret"
