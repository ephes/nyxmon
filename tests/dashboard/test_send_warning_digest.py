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


@pytest.mark.parametrize(
    "epoch,expected",
    [
        (9_000_000_000_000_000_000, "unknown"),
        (253_402_300_800, "unknown"),  # First second beyond datetime's UTC range.
        (2_147_483_647, "2038-01-19"),
        (1, "1970-01-01"),
        (0, "unknown"),
    ],
)
def test_invalid_since_is_isolated_without_mutating_cadence(
    make_check, caplog, epoch, expected
):
    check = make_check("a boundary", warning(), first_failure_at=epoch)
    make_check("b normal", warning(), first_failure_at=1793491200)
    before = list(CheckNotificationState.objects.order_by("pk").values())
    with patch(SEND, new_callable=AsyncMock, return_value=True) as send:
        assert "Sent a digest of 2" in run()
    text = send.await_args.args[0].replace("\\", "")
    assert f"a boundary (since {expected})" in text
    assert "b normal (since 2026-11-01)" in text
    assert list(CheckNotificationState.objects.order_by("pk").values()) == before
    if epoch > 253_402_300_799:
        assert f"Invalid first_failure_at for check {check.pk}" in caplog.text
        assert str(epoch) not in caplog.text


@pytest.mark.parametrize("error", [OSError, OverflowError, ValueError])
def test_platform_timestamp_conversion_failure_does_not_abort_digest(make_check, error):
    make_check("synthetic warning", warning(), first_failure_at=1793491200)
    with (
        patch("nyxboard.timestamps.datetime") as clock,
        patch(SEND, new_callable=AsyncMock, return_value=True) as send,
    ):
        clock.fromtimestamp.side_effect = error("synthetic conversion failure")
        assert "Sent a digest of 1" in run()
    assert "since unknown" in send.await_args.args[0]


@pytest.mark.parametrize("value", [True, "123", -1, 0, 0.5, float("nan"), float("inf")])
def test_epoch_policy_rejects_malformed_or_nonpositive_values(value):
    from nyxboard.timestamps import usable_epoch

    assert usable_epoch(value) is None


def test_epoch_upper_bound_is_checked_before_platform_conversion():
    from datetime import timezone

    from nyxboard.timestamps import MAX_EPOCH, usable_epoch

    with patch("nyxboard.timestamps.datetime") as clock:
        assert usable_epoch(MAX_EPOCH) == MAX_EPOCH
        assert usable_epoch(MAX_EPOCH + 1) is None
    clock.fromtimestamp.assert_called_once_with(MAX_EPOCH, tz=timezone.utc)


@pytest.mark.parametrize(
    "latest,backdated",
    [
        (ResultStatus.OK, ResultStatus.WARNING),
        (ResultStatus.WARNING, ResultStatus.OK),
    ],
)
@pytest.mark.parametrize("tie", [False, True])
def test_digest_uses_timestamp_then_id_like_dashboard(
    make_check, latest, backdated, tie
):
    from datetime import UTC, datetime, timedelta
    from nyxmon.domain import StatusChoices

    check = make_check("ordered")
    first = Result.objects.create(health_check=check, status=latest, data={})
    second = Result.objects.create(health_check=check, status=backdated, data={})
    now = datetime(2026, 1, 2, tzinfo=UTC)
    Result.objects.filter(pk=first.pk).update(created_at=now)
    Result.objects.filter(pk=second.pk).update(
        created_at=now if tie else now - timedelta(days=1)
    )
    expected_warning = (backdated if tie else latest) == ResultStatus.WARNING
    assert bool(open_warnings()) is expected_warning
    assert (check.get_status() == StatusChoices.WARNING) is expected_warning
    with patch(SEND, new_callable=AsyncMock) as send:
        output = run(dry_run=True)
    assert ("1 open warning" in output) is expected_warning
    send.assert_not_awaited()


def test_digest_bulk_query_only_loads_latest_warning_payloads(
    make_check, django_assert_num_queries
):
    expected = []
    for index in range(200):
        name = f"synthetic {index:03}"
        check = make_check(name, warning("old payload"), first_failure_at=1793491200)
        Result.objects.bulk_create(
            [
                Result(
                    health_check=check,
                    status=ResultStatus.WARNING,
                    data={"old": "x" * 100},
                )
                for _ in range(20)
            ]
        )
        status = ResultStatus.WARNING if index % 3 == 0 else ResultStatus.OK
        Result.objects.create(
            health_check=check, status=status, data={"error_msg": name}
        )
        if status == ResultStatus.WARNING:
            expected.append(OpenWarning(name, "2026-11-01", name))
    # No model hydration (including unrelated historical/configuration payloads).
    with (
        patch.object(Result, "from_db", side_effect=AssertionError("result hydration")),
        patch.object(
            HealthCheck, "from_db", side_effect=AssertionError("check hydration")
        ),
        django_assert_num_queries(1) as queries,
    ):
        assert open_warnings() == expected
    from django.db import connection

    with connection.cursor() as cursor:
        plan = cursor.execute("EXPLAIN QUERY PLAN " + queries[0]["sql"]).fetchall()
    # Histories belong inside scalar subqueries, never the outer scan.
    outer_scans = [row[3] for row in plan if row[1] == 0 and row[3].startswith("SCAN")]
    assert outer_scans == ["SCAN health_check"]


@pytest.mark.django_db(transaction=True)
def test_digest_observes_one_incident_generation(tmp_path):
    import sqlite3
    from django.db import connection, connections

    service = Service.objects.create(name="snapshot")
    check = HealthCheck.objects.create(
        service=service, name="snapshot", url="https://example.invalid"
    )
    result = Result.objects.create(
        health_check=check,
        status=ResultStatus.WARNING,
        data={"error_msg": "old incident"},
    )
    CheckNotificationState.objects.create(
        health_check=check, first_failure_at=1793491200
    )
    path = tmp_path / "snapshot.sqlite3"
    with sqlite3.connect(path) as copied:
        connection.connection.backup(copied)
        copied.execute("PRAGMA journal_mode=WAL")
    alias = "digest_snapshot"
    connections.databases[alias] = {**connection.settings_dict, "NAME": str(path)}
    reader = connections[alias]
    reader.connect()
    changed = False

    def reopen_after_first_read(execute, sql, params, many, context):
        nonlocal changed
        answer = execute(sql, params, many, context)
        if sql.lstrip().upper().startswith("SELECT") and not changed:
            changed = True
            with sqlite3.connect(path) as writer:
                # Close and reopen atomically, as the result publisher does.
                writer.execute(
                    "UPDATE check_result SET status='ok' WHERE id=?", (result.pk,)
                )
                writer.execute(
                    "INSERT INTO check_result (health_check_id,status,created_at,data) VALUES (?, 'warning', '2099-01-01', ?)",
                    (check.pk, '{"error_msg":"new incident"}'),
                )
                writer.execute(
                    "UPDATE check_notification_state SET first_failure_at=1793577600 WHERE check_id=?",
                    (check.pk,),
                )
        return answer

    try:
        with (
            patch.object(Result, "objects", Result.objects.db_manager(alias)),
            patch.object(HealthCheck, "objects", HealthCheck.objects.db_manager(alias)),
            patch.object(
                CheckNotificationState,
                "objects",
                CheckNotificationState.objects.db_manager(alias),
            ),
            reader.execute_wrapper(reopen_after_first_read),
        ):
            observed = open_warnings()
        assert changed
        assert observed in (
            [OpenWarning("snapshot", "2026-11-01", "old incident")],
            [OpenWarning("snapshot", "2026-11-02", "new incident")],
        )
    finally:
        reader.close()
        del connections[alias]
        del connections.databases[alias]
