"""Tests for the weekly warning digest command."""

from io import StringIO
from unittest.mock import AsyncMock, patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from nyxboard.management.commands.send_warning_digest import (
    MAX_MESSAGE_CHARS,
    OpenWarning,
    open_warnings,
    render,
)
from nyxboard.models import CheckNotificationState, HealthCheck, Result, Service
from nyxmon.domain import ResultStatus

SEND = (
    "nyxboard.management.commands.send_warning_digest.AsyncTelegramNotifier.async_send"
)


@pytest.fixture
def make_check(db):
    service = Service.objects.create(name="digest test service")

    def make(name, *results, disabled=False, first_failure_at=None):
        check = HealthCheck.objects.create(
            service=service,
            name=name,
            check_type="http",
            url="https://example.invalid",
            disabled=disabled,
        )
        for status, data in results:
            Result.objects.create(health_check=check, status=status, data=data)
        if first_failure_at is not None:
            CheckNotificationState.objects.create(
                health_check=check, first_failure_at=first_failure_at
            )
        return check

    return make


def warning(message="disk nearly full"):
    return ResultStatus.WARNING, {"error_msg": message}


def run(**options):
    out = StringIO()
    call_command("send_warning_digest", stdout=out, **options)
    return out.getvalue()


def test_only_a_newest_active_warning_on_an_enabled_check_is_open(make_check):
    make_check("a open", warning(), first_failure_at=1793491200)
    make_check("recovered", warning(), (ResultStatus.OK, {}))
    make_check("escalated", warning(), (ResultStatus.ERROR, {"error_msg": "down"}))
    make_check("disabled", warning(), disabled=True)
    make_check("no results")
    make_check("suppressed", (ResultStatus.WARNING, {"notification_suppressed": True}))
    make_check("internal", (ResultStatus.WARNING, {"collector_internal": True}))
    make_check("b no state", warning("other"))

    assert open_warnings() == [
        OpenWarning("a open", "2026-11-01", "disk nearly full"),
        OpenWarning("b no state", "unknown", "other"),
    ]


def test_nothing_is_sent_without_an_open_warning(make_check):
    make_check("fine", (ResultStatus.OK, {}))
    with patch(SEND, new_callable=AsyncMock) as send:
        assert "nothing to send" in run()
    send.assert_not_awaited()


def test_digest_is_silent_and_escaped(make_check, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    make_check("Software health (macmini)", warning("1 warning: $.a.b"))
    with patch(SEND, new_callable=AsyncMock, return_value=True) as send:
        assert "Sent a digest of 1" in run()
    text = send.await_args.args[0]
    assert "Software health \\(macmini\\)" in text
    assert "1 warning: $\\.a\\.b" in text
    assert send.await_args.kwargs["high_priority"] is False


def test_dry_run_prints_and_sends_nothing(make_check):
    make_check("standing", warning())
    with patch(SEND, new_callable=AsyncMock) as send:
        output = run(dry_run=True)
    assert "1 open warning" in output
    assert "standing" in output
    send.assert_not_awaited()


@pytest.mark.parametrize("delivered", [False, None])
def test_an_undelivered_digest_fails_the_command(make_check, delivered):
    make_check("standing", warning())
    with (
        patch(SEND, new_callable=AsyncMock, return_value=delivered),
        pytest.raises(CommandError),
    ):
        run()


def test_long_digest_stays_in_the_limit_and_counts_the_rest():
    warnings = [OpenWarning(f"check {i}", "2026-10-01", "x" * 200) for i in range(100)]
    text = render(warnings)
    assert len(text) <= MAX_MESSAGE_CHARS + 100
    assert text.startswith("⚠️ Nyxmon digest: 100 open warnings")
    assert "more in NyxBoard" in text
