"""Bounded, persisted collector/batch-level incident alerting.

The 2026-09-01 incident produced 36 Telegram notifications for a single wedged
batch because every reclaimed processing lease forced its own immediate alert.
These tests pin the replacement contract:

* reclaiming N checks recovers and reschedules all N but produces at most one
  external notification, and never one per check;
* a wedged executor produces one clear collector-level alert plus elapsed-time
  reminders while execution stays paused;
* incident identity is persisted, so neither further collector iterations nor a
  service restart re-alert an incident that is already open;
* an incident can resolve, after which a genuinely new incident alerts again.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from nyxmon.adapters.collector import (
    ABANDONED_BATCH_REMINDER_SECONDS,
    ABANDONED_BATCH_RETRY_SECONDS,
    COLLECTOR_PAUSED_INCIDENT_KEY,
    COLLECTOR_STALE_BATCH_INCIDENT_KEY,
    STALE_BATCH_REMINDER_SECONDS,
    STALE_BATCH_RESOLVE_AFTER_SECONDS,
    AsyncCheckCollector,
)
from nyxmon.adapters.repositories import InMemoryStore, SqliteStore
from nyxmon.adapters.repositories.interface import NotificationState
from nyxmon.bootstrap import bootstrap
from nyxmon.domain import Check, CheckStatus, CheckType, Result, ResultStatus
from nyxmon.domain.commands import AddCheckResult
from nyxmon.domain.models import CheckResult

LEASE_SECONDS = 900


def _wedged_check(check_id: int, *, data: dict[str, Any] | None = None) -> Check:
    """A check whose processing lease expired long ago."""
    now = int(time.time())
    return Check(
        check_id=check_id,
        service_id=1,
        name=f"wedged-{check_id}",
        check_type=CheckType.HTTP,
        url="https://example.test/health",
        check_interval=300,
        next_check_time=now + 300,
        processing_started_at=now - 10 * LEASE_SECONDS,
        status=CheckStatus.PROCESSING,
        data=data if data is not None else {},
    )


def _freeze_clock(monkeypatch, start: int = 1_700_000_000) -> dict[str, int]:
    clock = {"now": start}
    monkeypatch.setattr(
        "nyxmon.adapters.collector.current_epoch", lambda: float(clock["now"])
    )
    return clock


def _wire(store, notifier=None):
    collector = AsyncCheckCollector()
    notifier = notifier or MagicMock()
    bus = bootstrap(store=store, collector=collector, notifier=notifier)
    return collector, bus, notifier


def _feed_result(bus, store, check_id: int, status: str) -> None:
    check = store.checks.get(check_id)
    result = Result(
        check_id=check_id,
        status=status,
        data={"error_msg": "boom"} if status != ResultStatus.OK else {},
    )
    if status == ResultStatus.OK:
        check.schedule_next_check()
    bus.handle(AddCheckResult(check_result=CheckResult(check=check, result=result)))


# ---------------------------------------------------------------- stale batch


@pytest.mark.anyio
async def test_legacy_stale_batch_alerts_once_and_recovers_every_check(
    monkeypatch,
) -> None:
    """36 expired leases: one collector alert, 36 checks reclaimed/rescheduled."""
    monkeypatch.delenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", raising=False)
    _freeze_clock(monkeypatch)
    store = InMemoryStore()
    collector, bus, notifier = _wire(store)
    stale_ids = list(range(1, 37))
    for check_id in stale_ids:
        store.checks.add(_wedged_check(check_id))

    # The reclaim batch is bounded, so the burst spans several iterations.
    for _ in range(10):
        await collector._collect_once()

    # 1. Every check was reclaimed and rescheduled, none left permanently
    #    processing.
    for check_id in stale_ids:
        check = store.checks.get(check_id)
        assert check.status == CheckStatus.IDLE
        assert check.processing_started_at == 0
        assert check.next_check_time > 0

    # 2. Per-check recovery history is still persisted ...
    stale_results = [
        result
        for result in store.results.list()
        if result.data.get("error_type") == "stale_processing_lease"
    ]
    assert len(stale_results) == 36

    # 3. ... but lease recovery never touches per-check alert state, so none of
    #    them can page now or drag a later real failure over the threshold.
    for check_id in stale_ids:
        assert store.checks.get_notification_state(check_id) == NotificationState()

    # 4. Exactly ONE external notification for the whole batch.
    assert notifier.notify_check_failed.call_count == 1
    incident_check, incident_result = notifier.notify_check_failed.call_args.args
    assert incident_check.check_id == 0
    assert incident_result.data["error_type"] == "stale_processing_lease_batch"
    assert incident_result.data["incident_key"] == COLLECTOR_STALE_BATCH_INCIDENT_KEY
    assert "36" in incident_result.data["error_msg"]

    # 5. The 28 checks that pass on their next execution are not represented as
    #    28 endpoint outages, and the 8 that do not still dampen normally.
    for check_id in stale_ids[:28]:
        _feed_result(bus, store, check_id, ResultStatus.OK)
    for check_id in stale_ids[28:35]:
        _feed_result(bus, store, check_id, ResultStatus.ERROR)
    _feed_result(bus, store, stale_ids[35], ResultStatus.WARNING)

    assert notifier.notify_check_failed.call_count == 1


@pytest.mark.anyio
async def test_stale_lease_never_pages_even_with_first_sample_policy(
    monkeypatch,
) -> None:
    """A per-check ``consecutive_failures: 1`` policy must not resurrect the storm."""
    _freeze_clock(monkeypatch)
    store = InMemoryStore()
    collector, bus, notifier = _wire(store)
    policy = {"notification_policy": {"consecutive_failures": 1}}
    for check_id in (1, 2, 3):
        store.checks.add(_wedged_check(check_id, data=dict(policy)))

    await collector._collect_once()

    # One collector-level incident, not three per-check pages.
    assert notifier.notify_check_failed.call_count == 1
    assert (
        notifier.notify_check_failed.call_args.args[1].data["error_type"]
        == "stale_processing_lease_batch"
    )

    # A genuine failure on the same check still pages on its first sample.
    _feed_result(bus, store, 1, ResultStatus.ERROR)
    assert notifier.notify_check_failed.call_count == 2
    assert notifier.notify_check_failed.call_args.args[0].check_id == 1


@pytest.mark.anyio
async def test_stale_batch_incident_reminds_on_elapsed_time_then_resolves(
    monkeypatch,
) -> None:
    clock = _freeze_clock(monkeypatch)
    store = InMemoryStore()
    collector, _bus, notifier = _wire(store)
    for check_id in (1, 2):
        store.checks.add(_wedged_check(check_id))

    await collector._collect_once()
    assert notifier.notify_check_failed.call_count == 1

    # Still stale a minute later: deduplicated, no second message.
    store.checks.get(1).status = CheckStatus.PROCESSING
    store.checks.get(1).processing_started_at = int(time.time()) - 10 * LEASE_SECONDS
    clock["now"] += 60
    await collector._collect_once()
    assert notifier.notify_check_failed.call_count == 1

    # Past the reminder window: exactly one reminder.
    store.checks.get(1).status = CheckStatus.PROCESSING
    store.checks.get(1).processing_started_at = int(time.time()) - 10 * LEASE_SECONDS
    clock["now"] += STALE_BATCH_REMINDER_SECONDS
    await collector._collect_once()
    assert notifier.notify_check_failed.call_count == 2

    # Quiet for long enough: the incident resolves.
    clock["now"] += STALE_BATCH_RESOLVE_AFTER_SECONDS
    await collector._collect_once()
    assert store.get_collector_incident(COLLECTOR_STALE_BATCH_INCIDENT_KEY) is None

    # A genuinely new later batch alerts again.
    store.checks.add(_wedged_check(9))
    clock["now"] += 60
    await collector._collect_once()
    assert notifier.notify_check_failed.call_count == 3


@pytest.mark.anyio
async def test_stale_batch_incident_is_deduplicated_across_a_restart(
    tmp_path, monkeypatch
) -> None:
    clock = _freeze_clock(monkeypatch)
    db_path = tmp_path / "collector.db"

    store = SqliteStore(db_path)
    collector, _bus, notifier = _wire(store)
    for check_id in (1, 2, 3):
        await store.checks._add_async(_wedged_check(check_id))
    await collector._collect_once()
    assert notifier.notify_check_failed.call_count == 1

    # Simulated service restart: brand new store, bus and collector over the
    # same database file. The in-memory ``_abandoned_batch_*`` fields are gone,
    # but the persisted incident is not.
    restarted_store = SqliteStore(db_path)
    restarted_collector, _bus2, restarted_notifier = _wire(restarted_store)
    for check_id in (4, 5):
        await restarted_store.checks._add_async(_wedged_check(check_id))
    clock["now"] += 30
    await restarted_collector._collect_once()

    assert restarted_notifier.notify_check_failed.call_count == 0
    incident = await restarted_store._get_collector_incident_async(
        COLLECTOR_STALE_BATCH_INCIDENT_KEY
    )
    assert incident is not None
    assert incident.alert_count == 1
    assert incident.payload["reclaimed_count"] == 5


# --------------------------------------------------------------- wedged batch


def _wedged_batch_collector(monkeypatch, release: threading.Event):
    monkeypatch.setattr(
        "nyxmon.adapters.collector.processing_lease_seconds", lambda: 0.05
    )
    monkeypatch.setattr("nyxmon.adapters.collector.RESULT_HANDLING_BUDGET_SECONDS", 0)
    collector = AsyncCheckCollector()
    incident_store = InMemoryStore()
    collector.set_incident_store(incident_store)
    due = Check(
        check_id=1,
        service_id=1,
        name="wedged",
        check_type=CheckType.HTTP,
        url="https://example.test/health",
        data={},
    )
    checks = MagicMock()
    checks.list_async = AsyncMock(return_value=[])
    checks.reclaim_stale_checks_async = AsyncMock(return_value=[])
    checks.list_due_checks_async = AsyncMock(return_value=[due])
    bus = MagicMock()
    bus.uow.store.checks = checks
    bus.handle.side_effect = lambda command: release.wait()
    recovery_handler = MagicMock(return_value=False)
    process_notifier = MagicMock()
    collector.set_message_bus(bus)
    collector.set_recovery_handler(recovery_handler)
    collector.set_process_notifier(process_notifier)
    return collector, bus, incident_store, recovery_handler, process_notifier


@pytest.mark.anyio
async def test_wedged_batch_alerts_once_then_reminds_on_elapsed_time(
    monkeypatch, caplog
) -> None:
    clock = _freeze_clock(monkeypatch)
    release = threading.Event()
    (
        collector,
        bus,
        incident_store,
        recovery_handler,
        process_notifier,
    ) = _wedged_batch_collector(monkeypatch, release)

    try:
        with anyio.fail_after(3):
            await collector._collect_once()  # arms the abandoned batch
            await collector._collect_once()  # one clear collector-level alert
            await collector._collect_once()  # deduplicated across iterations
            clock["now"] += 60
            await collector._collect_once()  # still inside the reminder window
            clock["now"] += ABANDONED_BATCH_REMINDER_SECONDS
            await collector._collect_once()  # bounded elapsed-time reminder

        # Execution stays paused: no second batch was ever dispatched.
        assert bus.handle.call_count == 1
        # No borrowed per-check alert: the incident is collector level only.
        recovery_handler.assert_not_called()

        assert process_notifier.call_count == 2
        check, result = process_notifier.call_args_list[0].args
        assert check.check_id == 0
        assert result.data["error_type"] == "collector_execution_paused"
        assert result.data["incident_key"] == COLLECTOR_PAUSED_INCIDENT_KEY
        assert result.status == ResultStatus.ERROR
        assert "new executions are paused" in caplog.text

        incident = incident_store.get_collector_incident(COLLECTOR_PAUSED_INCIDENT_KEY)
        assert incident is not None
        assert incident.alert_count == 2
    finally:
        release.set()


@pytest.mark.anyio
async def test_wedged_batch_resolves_and_a_later_wedge_alerts_again(
    monkeypatch,
) -> None:
    clock = _freeze_clock(monkeypatch)
    release = threading.Event()
    (
        collector,
        bus,
        incident_store,
        _recovery_handler,
        process_notifier,
    ) = _wedged_batch_collector(monkeypatch, release)

    try:
        with anyio.fail_after(3):
            await collector._collect_once()
            await collector._collect_once()
            assert process_notifier.call_count == 1

            release.set()
            while (
                collector._abandoned_batch_done is not None
                and not collector._abandoned_batch_done.is_set()
            ):
                await anyio.sleep(0.01)
            await collector._collect_once()

        assert (
            incident_store.get_collector_incident(COLLECTOR_PAUSED_INCIDENT_KEY) is None
        )

        # A genuinely new incident alerts again, well inside the reminder window
        # of the resolved one.
        release.clear()
        clock["now"] += 5
        with anyio.fail_after(3):
            await collector._collect_once()
            await collector._collect_once()
        assert process_notifier.call_count == 2
    finally:
        release.set()


@pytest.mark.anyio
async def test_paused_incident_from_a_previous_process_is_resolved_on_startup(
    monkeypatch,
) -> None:
    clock = _freeze_clock(monkeypatch)
    incident_store = InMemoryStore()
    incident_store.claim_collector_incident_alert(
        COLLECTOR_PAUSED_INCIDENT_KEY,
        now=clock["now"] - 10,
        reminder_seconds=ABANDONED_BATCH_REMINDER_SECONDS,
        payload={"incident_type": "collector_execution_paused"},
    )
    collector = AsyncCheckCollector()
    collector.set_incident_store(incident_store)
    checks = MagicMock()
    checks.list_async = AsyncMock(return_value=[])
    checks.reclaim_stale_checks_async = AsyncMock(return_value=[])
    checks.list_due_checks_async = AsyncMock(return_value=[])
    bus = MagicMock()
    bus.uow.store.checks = checks
    collector.set_message_bus(bus)
    process_notifier = MagicMock()
    collector.set_process_notifier(process_notifier)

    await collector._collect_once()

    # The wedged thread died with the previous process, so the incident is
    # resolved rather than reminded about forever.
    assert incident_store.get_collector_incident(COLLECTOR_PAUSED_INCIDENT_KEY) is None
    process_notifier.assert_not_called()


# ------------------------------------------------- fencing and deletion safety


@pytest.mark.anyio
async def test_stale_recovery_cannot_release_a_newer_claim(monkeypatch) -> None:
    _freeze_clock(monkeypatch)
    store = InMemoryStore()
    collector, _bus, notifier = _wire(store)
    store.checks.add(_wedged_check(1))
    reclaimed = (await store.checks.reclaim_stale_checks_async(LEASE_SECONDS))[0]

    # A fresh worker claims the check before the recovery result lands.
    live = store.checks.get(1)
    live.status = CheckStatus.PROCESSING
    live.processing_started_at = int(time.time())

    assert await collector._record_stale_check(reclaimed) is True

    assert store.checks.get(1).status == CheckStatus.PROCESSING
    assert store.checks.get(1).processing_started_at == live.processing_started_at
    assert len(store.results.list()) == 1
    assert store.checks.get_notification_state(1) == NotificationState()
    notifier.notify_check_failed.assert_not_called()


@pytest.mark.anyio
async def test_stale_recovery_does_not_resurrect_a_deleted_check(tmp_path) -> None:
    db_path = tmp_path / "collector.db"
    store = SqliteStore(db_path)
    collector, _bus, notifier = _wire(store)
    await store.checks._add_async(_wedged_check(1))
    reclaimed = (await store.checks.reclaim_stale_checks_async(LEASE_SECONDS))[0]

    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM health_check WHERE id = 1")
        connection.commit()

    assert await collector._record_stale_check(reclaimed) is True

    assert await store.results._list_async() == []
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT COUNT(*) FROM health_check").fetchone()
    assert rows[0] == 0
    notifier.notify_check_failed.assert_not_called()


@pytest.mark.anyio
async def test_a_failed_incident_alert_is_retried_and_not_silently_resolved(
    monkeypatch,
) -> None:
    """Regression: a one-off stale batch whose first send fails must still page.

    `_record_incident_send()` marks the incident for retry, but only the branch
    that handles freshly-recovered leases ever claimed an alert. A one-off batch
    produces no further reclaims, so the retry never fired and the incident was
    closed after STALE_BATCH_RESOLVE_AFTER_SECONDS having notified nobody.
    """
    monkeypatch.delenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", raising=False)
    clock = _freeze_clock(monkeypatch)
    store = InMemoryStore()
    notifier = MagicMock()
    notifier.notify_check_failed.side_effect = RuntimeError("telegram unreachable")
    collector, _bus, _n = _wire(store, notifier)
    store.checks.add(_wedged_check(1))

    # First iteration: the batch is detected, but delivery fails.
    await collector._collect_once()
    assert notifier.notify_check_failed.call_count == 1
    assert COLLECTOR_STALE_BATCH_INCIDENT_KEY in collector._incident_retry_keys

    # Delivery recovers. No new stale leases appear - this is a one-off batch.
    notifier.notify_check_failed.side_effect = None
    clock["now"] += ABANDONED_BATCH_RETRY_SECONDS + 1
    await collector._collect_once()

    assert notifier.notify_check_failed.call_count == 2, (
        "the failed incident alert was never retried"
    )
    assert COLLECTOR_STALE_BATCH_INCIDENT_KEY not in collector._incident_retry_keys

    # Only now, with the alert actually delivered, may the incident resolve.
    clock["now"] += STALE_BATCH_RESOLVE_AFTER_SECONDS + 1
    await collector._collect_once()
    assert await collector._incident_get(COLLECTOR_STALE_BATCH_INCIDENT_KEY) is None


@pytest.mark.anyio
async def test_an_undelivered_incident_is_never_resolved(monkeypatch) -> None:
    """While delivery keeps failing the incident stays open, not silently closed."""
    monkeypatch.delenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", raising=False)
    clock = _freeze_clock(monkeypatch)
    store = InMemoryStore()
    notifier = MagicMock()
    notifier.notify_check_failed.side_effect = RuntimeError("telegram unreachable")
    collector, _bus, _n = _wire(store, notifier)
    store.checks.add(_wedged_check(1))

    await collector._collect_once()
    clock["now"] += STALE_BATCH_RESOLVE_AFTER_SECONDS * 3
    await collector._collect_once()

    incident = await collector._incident_get(COLLECTOR_STALE_BATCH_INCIDENT_KEY)
    assert incident is not None, (
        "incident resolved while its alert had never been delivered"
    )


@pytest.mark.anyio
async def test_failed_delivery_survives_a_worker_restart(monkeypatch, tmp_path) -> None:
    """Regression: the retry marker must be durable, not process-local.

    `_incident_claim()` stamps `last_alert_at` BEFORE the notification is
    attempted. When the failed-send marker lived only in `_incident_retry_keys`,
    a restart lost it: the persisted incident looked recently alerted, so no
    retry fired and it resolved after the quiet period having paged nobody -
    in a change set whose whole premise is that incident state survives
    restarts.
    """
    clock = _freeze_clock(monkeypatch)
    db = tmp_path / "nyxmon.sqlite3"

    # --- process 1: the batch is detected, delivery fails, process dies ---
    store = SqliteStore(db)
    notifier = MagicMock()
    notifier.notify_check_failed.side_effect = RuntimeError("telegram unreachable")
    collector, _bus, _n = _wire(store, notifier)
    store.checks.add(_wedged_check(1))

    await collector._collect_once()
    assert notifier.notify_check_failed.call_count == 1

    persisted = await collector._incident_get(COLLECTOR_STALE_BATCH_INCIDENT_KEY)
    assert persisted is not None
    assert persisted.payload.get("delivery_pending") is True, (
        "the failed delivery was not persisted, so a restart cannot retry it"
    )

    # --- process 2: fresh collector, empty in-memory state, delivery works ---
    store2 = SqliteStore(db)
    notifier2 = MagicMock()
    collector2, _bus2, _n2 = _wire(store2, notifier2)
    assert collector2._incident_retry_keys == set()

    clock["now"] += ABANDONED_BATCH_RETRY_SECONDS + 1
    await collector2._collect_once()

    assert notifier2.notify_check_failed.call_count == 1, (
        "a restart lost the retry marker and the alert was never delivered"
    )
    reloaded = await collector2._incident_get(COLLECTOR_STALE_BATCH_INCIDENT_KEY)
    assert reloaded is not None
    assert "delivery_pending" not in reloaded.payload
