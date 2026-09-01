"""Elapsed-time reminder cadence, restart durability, and bootstrap semantics."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from anyio.from_thread import BlockingPortalProvider

from nyxmon.adapters.repositories import InMemoryStore, SqliteStore
from nyxmon.adapters.repositories.interface import NotificationState
from nyxmon.adapters.repositories.sqlite_repo import SqliteCheckRepository
from nyxmon.domain.commands import AddCheckResult
from nyxmon.domain.models import (
    Check,
    CheckResult,
    CheckType,
    Result,
    ResultStatus,
)
from nyxmon.service_layer.handlers import add_check_result
from nyxmon.service_layer.notification_policy import reset_policy_warning_state
from nyxmon.service_layer.unit_of_work import UnitOfWork


class StubNotifier:
    def __init__(self) -> None:
        self.failed_notifications: list[tuple[Check, Result, int]] = []
        self.clock: Any = None

    def notify_check_failed(self, check: Check, result: Result) -> None:
        now = int(self.clock()) if self.clock is not None else 0
        self.failed_notifications.append((check, result, now))

    def notify_service_status_changed(self, service: Any, status: str) -> None:
        del service, status

    def epochs_for(self, check_id: int) -> list[int]:
        return [
            now
            for check, _, now in self.failed_notifications
            if check.check_id == check_id
        ]


class Clock:
    """Explicit, injectable clock: no sleeps, no wall-clock reads."""

    def __init__(self, start: int = 1_700_000_000) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += seconds


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        db_path = Path(handle.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


def _build_check(
    check_id: int = 1,
    *,
    interval: int = 300,
    data: dict[str, Any] | None = None,
) -> Check:
    return Check(
        check_id=check_id,
        service_id=1,
        name=f"Check {check_id}",
        check_type=CheckType.HTTP,
        url="https://example.test/health",
        check_interval=interval,
        data=data if data is not None else {},
    )


def _submit(uow: UnitOfWork, notifier: StubNotifier, check: Check, status: str) -> None:
    result = Result(check_id=check.check_id, status=status, data={})
    add_check_result(
        AddCheckResult(check_result=CheckResult(check=check, result=result)),
        uow,
        notifier,
    )


# --------------------------------------------------------------------------
# 1. Reminder cadence is elapsed-time based, not sample-count based.
# --------------------------------------------------------------------------


def test_reminders_are_interval_independent(monkeypatch) -> None:
    """A 5-minute and a 1-hour check with the same reminder duration remind
    at approximately the same elapsed times."""
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setenv("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "3600")
    monkeypatch.delenv("NYXMON_NOTIFY_REPEAT_FAILURES", raising=False)
    clock = Clock()
    monkeypatch.setattr("nyxmon.service_layer.handlers.current_epoch", clock)

    store = InMemoryStore()
    uow = UnitOfWork(store=store)
    notifier = StubNotifier()
    notifier.clock = clock

    fast = _build_check(1, interval=300)
    slow = _build_check(2, interval=3600)
    store.checks.add(fast)
    store.checks.add(slow)

    start = clock.now
    due = {1: start, 2: start}
    end = start + 4 * 3600
    while clock.now <= end:
        for check in (fast, slow):
            if clock.now >= due[check.check_id]:
                _submit(uow, notifier, check, ResultStatus.ERROR)
                due[check.check_id] += check.check_interval
        clock.advance(60)

    fast_epochs = [epoch - start for epoch in notifier.epochs_for(1)]
    slow_epochs = [epoch - start for epoch in notifier.epochs_for(2)]

    # initial alert + one reminder per elapsed hour
    assert len(fast_epochs) == 5
    assert len(slow_epochs) == 5
    for fast_at, slow_at in zip(fast_epochs, slow_epochs):
        assert abs(fast_at - slow_at) <= 3600  # within one sample of the slow check
    # The slow check must not be starved: last reminder is inside the window.
    assert slow_epochs[-1] >= 3 * 3600


def test_sample_count_alone_never_triggers_a_reminder(monkeypatch) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setenv("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "3600")
    clock = Clock()
    monkeypatch.setattr("nyxmon.service_layer.handlers.current_epoch", clock)
    store = InMemoryStore()
    uow = UnitOfWork(store=store)
    notifier = StubNotifier()
    notifier.clock = clock
    check = _build_check(1)
    store.checks.add(check)

    for _ in range(50):
        _submit(uow, notifier, check, ResultStatus.ERROR)

    assert len(notifier.failed_notifications) == 1


def test_deprecated_repeat_failures_is_ignored_with_a_warning(
    monkeypatch, caplog
) -> None:
    reset_policy_warning_state()
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setenv("NYXMON_NOTIFY_REPEAT_FAILURES", "3")
    monkeypatch.delenv("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", raising=False)
    clock = Clock()
    monkeypatch.setattr("nyxmon.service_layer.handlers.current_epoch", clock)
    store = InMemoryStore()
    uow = UnitOfWork(store=store)
    notifier = StubNotifier()
    notifier.clock = clock
    check = _build_check(1)
    store.checks.add(check)

    with caplog.at_level("WARNING"):
        for _ in range(10):
            _submit(uow, notifier, check, ResultStatus.ERROR)
            clock.advance(300)

    # 10 samples * 5 min = 50 min elapsed, far below the 6h default reminder.
    assert len(notifier.failed_notifications) == 1
    assert "NYXMON_NOTIFY_REPEAT_FAILURES" in caplog.text
    assert "NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS" in caplog.text


# --------------------------------------------------------------------------
# 2. Per-check policy overrides
# --------------------------------------------------------------------------


def test_per_check_policy_overrides_threshold_and_reminder(monkeypatch) -> None:
    monkeypatch.delenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", raising=False)
    monkeypatch.setenv("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "21600")
    clock = Clock()
    monkeypatch.setattr("nyxmon.service_layer.handlers.current_epoch", clock)
    store = InMemoryStore()
    uow = UnitOfWork(store=store)
    notifier = StubNotifier()
    notifier.clock = clock
    check = _build_check(
        1,
        data={
            "notification_policy": {
                "consecutive_failures": 1,
                "reminder_seconds": 600,
            }
        },
    )
    store.checks.add(check)

    _submit(uow, notifier, check, ResultStatus.ERROR)
    assert len(notifier.failed_notifications) == 1  # first sample pages

    clock.advance(599)
    _submit(uow, notifier, check, ResultStatus.ERROR)
    assert len(notifier.failed_notifications) == 1

    clock.advance(1)
    _submit(uow, notifier, check, ResultStatus.ERROR)
    assert len(notifier.failed_notifications) == 2


@pytest.mark.parametrize(
    "policy",
    [
        "not-a-dict",
        {"consecutive_failures": 0},
        {"consecutive_failures": True},
        {"consecutive_failures": "many"},
        {"reminder_seconds": -1},
        {"reminder_seconds": None},
        {"consecutive_failures": 10**9},
    ],
)
def test_malformed_per_check_policy_falls_back_to_global_default(
    monkeypatch, caplog, policy
) -> None:
    monkeypatch.delenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", raising=False)
    clock = Clock()
    monkeypatch.setattr("nyxmon.service_layer.handlers.current_epoch", clock)
    store = InMemoryStore()
    uow = UnitOfWork(store=store)
    notifier = StubNotifier()
    notifier.clock = clock
    check = _build_check(1, data={"notification_policy": policy})
    store.checks.add(check)
    reset_policy_warning_state()

    with caplog.at_level("WARNING"):
        _submit(uow, notifier, check, ResultStatus.ERROR)
        assert notifier.failed_notifications == []  # global default threshold is 2
        _submit(uow, notifier, check, ResultStatus.ERROR)

    assert len(notifier.failed_notifications) == 1
    assert "notification_policy" in caplog.text
    # A malformed policy warns once, not once per sample.
    assert caplog.text.count("using the global default") == 1


def test_warning_results_use_the_warning_reminder_cadence(monkeypatch) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setenv("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("NYXMON_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS", "86400")
    clock = Clock()
    monkeypatch.setattr("nyxmon.service_layer.handlers.current_epoch", clock)
    store = InMemoryStore()
    uow = UnitOfWork(store=store)
    notifier = StubNotifier()
    notifier.clock = clock
    check = _build_check(1)
    store.checks.add(check)

    _submit(uow, notifier, check, ResultStatus.WARNING)
    assert len(notifier.failed_notifications) == 1

    clock.advance(3600 * 5)
    _submit(uow, notifier, check, ResultStatus.WARNING)
    assert len(notifier.failed_notifications) == 1  # warnings remind daily

    clock.advance(86400)
    _submit(uow, notifier, check, ResultStatus.WARNING)
    assert len(notifier.failed_notifications) == 2


# --------------------------------------------------------------------------
# 3. Recovery starts a new incident
# --------------------------------------------------------------------------


def test_recovered_check_starts_a_new_incident(monkeypatch) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setenv("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "86400")
    clock = Clock()
    monkeypatch.setattr("nyxmon.service_layer.handlers.current_epoch", clock)
    store = InMemoryStore()
    uow = UnitOfWork(store=store)
    notifier = StubNotifier()
    notifier.clock = clock
    check = _build_check(1)
    store.checks.add(check)

    _submit(uow, notifier, check, ResultStatus.ERROR)
    assert len(notifier.failed_notifications) == 1

    clock.advance(300)
    _submit(uow, notifier, check, ResultStatus.OK)
    assert store.checks.get_notification_state(1) == NotificationState()

    clock.advance(300)
    _submit(uow, notifier, check, ResultStatus.ERROR)
    # New incident pages immediately even though the reminder window is a day.
    assert len(notifier.failed_notifications) == 2


# --------------------------------------------------------------------------
# 4. Restart durability
# --------------------------------------------------------------------------


def test_notification_state_survives_a_simulated_service_restart(
    monkeypatch, temp_db
) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setenv("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "3600")
    clock = Clock()
    monkeypatch.setattr("nyxmon.service_layer.handlers.current_epoch", clock)
    notifier = StubNotifier()
    notifier.clock = clock
    check = _build_check(1)

    portal_provider = BlockingPortalProvider()
    with portal_provider as portal:
        store = SqliteStore(temp_db)
        store.set_portal_provider(portal_provider)
        portal.call(store.checks._add_async, check)
        uow = UnitOfWork(store=store)

        _submit(uow, notifier, check, ResultStatus.ERROR)
        assert len(notifier.failed_notifications) == 1

        # --- simulated restart: brand-new store objects over the same file ---
        clock.advance(600)
        restarted = SqliteStore(temp_db)
        restarted.set_portal_provider(portal_provider)
        restarted_uow = UnitOfWork(store=restarted)

        state = restarted.checks.get_notification_state(1)
        assert state.failure_count == 1
        assert state.last_notified_at == clock.now - 600

        _submit(restarted_uow, notifier, check, ResultStatus.ERROR)
        assert len(notifier.failed_notifications) == 1  # no re-alert after restart

        clock.advance(3000)
        _submit(restarted_uow, notifier, check, ResultStatus.ERROR)
        assert len(notifier.failed_notifications) == 2  # reminder honours elapsed time


def test_results_retention_cleanup_does_not_reset_reminder_timing(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setenv("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "3600")
    clock = Clock()
    monkeypatch.setattr("nyxmon.service_layer.handlers.current_epoch", clock)
    store = InMemoryStore()
    uow = UnitOfWork(store=store)
    notifier = StubNotifier()
    notifier.clock = clock
    check = _build_check(1)
    store.checks.add(check)

    _submit(uow, notifier, check, ResultStatus.ERROR)
    assert len(notifier.failed_notifications) == 1

    store.results.results.clear()
    store.results._timestamps.clear()

    clock.advance(600)
    _submit(uow, notifier, check, ResultStatus.ERROR)
    assert len(notifier.failed_notifications) == 1

    clock.advance(3000)
    _submit(uow, notifier, check, ResultStatus.ERROR)
    assert len(notifier.failed_notifications) == 2


# --------------------------------------------------------------------------
# 5. Bootstrap / upgrade from the d782f75 schema
# --------------------------------------------------------------------------


LEGACY_SCHEMA = """
CREATE TABLE health_check (
    id               INTEGER PRIMARY KEY,
    service_id       INTEGER NOT NULL,
    name             TEXT    DEFAULT '',
    check_type       TEXT    NOT NULL,
    url              TEXT    NOT NULL,
    check_interval   INTEGER NOT NULL,
    status           TEXT    DEFAULT 'idle',
    next_check_time  INTEGER DEFAULT 0,
    processing_started_at INTEGER DEFAULT 0,
    disabled         INTEGER DEFAULT 0,
    data             TEXT    DEFAULT '{}'
);
CREATE TABLE check_notification_state (
    check_id           INTEGER PRIMARY KEY
                       REFERENCES health_check(id) ON DELETE CASCADE,
    failure_count      INTEGER NOT NULL DEFAULT 0,
    last_attempt_count INTEGER NOT NULL DEFAULT 0,
    last_immediate_at  INTEGER NOT NULL DEFAULT 0
);
"""


def _seed_legacy_db(db_path: Path) -> None:
    with sqlite3.connect(db_path) as db:
        db.executescript(LEGACY_SCHEMA)
        db.executemany(
            """INSERT INTO health_check
               (id, service_id, name, check_type, url, check_interval,
                status, next_check_time, processing_started_at, disabled, data)
               VALUES (?, 1, ?, 'http', 'https://example.test/x', 3600,
                       'idle', 0, 0, 0, '{}')""",
            [(1, "already alerting"), (2, "healthy"), (3, "failing below threshold")],
        )
        db.executemany(
            """INSERT INTO check_notification_state
               (check_id, failure_count, last_attempt_count, last_immediate_at)
               VALUES (?, ?, ?, ?)""",
            [(1, 121, 121, 0), (2, 0, 0, 0), (3, 1, 0, 0)],
        )
        db.commit()


@pytest.mark.anyio
async def test_worker_schema_upgrade_backfills_notified_timestamps(
    temp_db, monkeypatch
) -> None:
    _seed_legacy_db(temp_db)
    monkeypatch.setattr(
        "nyxmon.adapters.repositories.sqlite_repo.current_epoch", lambda: 1_700_000_000
    )
    repo = SqliteCheckRepository(temp_db)

    assert await repo._get_notification_state_async(1) == NotificationState(
        failure_count=121,
        last_attempt_count=121,
        last_immediate_at=0,
        last_notified_at=1_700_000_000,
        first_failure_at=1_700_000_000,
    )
    # An OK check stays pristine.
    assert await repo._get_notification_state_async(2) == NotificationState()
    # A streak that never reached the alert threshold is not marked as notified.
    assert await repo._get_notification_state_async(3) == NotificationState(
        failure_count=1,
        last_attempt_count=0,
        last_immediate_at=0,
        last_notified_at=0,
        first_failure_at=1_700_000_000,
    )


def test_bootstrapped_failures_do_not_page_as_new_incidents(
    monkeypatch, temp_db
) -> None:
    _seed_legacy_db(temp_db)
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setenv("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "3600")
    clock = Clock()
    monkeypatch.setattr("nyxmon.service_layer.handlers.current_epoch", clock)
    monkeypatch.setattr("nyxmon.adapters.repositories.sqlite_repo.current_epoch", clock)
    notifier = StubNotifier()
    notifier.clock = clock

    portal_provider = BlockingPortalProvider()
    with portal_provider:
        store = SqliteStore(temp_db)
        store.set_portal_provider(portal_provider)
        uow = UnitOfWork(store=store)

        established = _build_check(1, interval=3600)
        _submit(uow, notifier, established, ResultStatus.ERROR)
        # Established failure is treated as already notified at bootstrap time.
        assert notifier.failed_notifications == []

        clock.advance(3600)
        _submit(uow, notifier, established, ResultStatus.ERROR)
        assert len(notifier.failed_notifications) == 1

        # A previously clean check still pages normally when it starts failing.
        healthy = _build_check(2, interval=3600)
        _submit(uow, notifier, healthy, ResultStatus.ERROR)
        assert len(notifier.epochs_for(2)) == 1


@pytest.mark.anyio
async def test_interrupted_schema_upgrade_does_not_lose_the_backfill(
    temp_db, monkeypatch
) -> None:
    """Regression: the column add and its backfill must commit atomically.

    An earlier revision ran each ``ALTER TABLE`` outside a transaction and
    backfilled only columns that call had added. A crash between the ALTER and
    its UPDATE therefore left the column present but unpopulated, and every
    later run skipped the adoption permanently - so a rollout re-paged every
    already-alerting streak, which is the exact alert storm this schema exists
    to prevent.
    """
    _seed_legacy_db(temp_db)

    boom = RuntimeError("process died mid-upgrade")
    real_execute = aiosqlite.Connection.execute

    async def fail_on_backfill(self, sql, *args, **kwargs):
        if sql.strip().upper().startswith("UPDATE CHECK_NOTIFICATION_STATE"):
            raise boom
        return await real_execute(self, sql, *args, **kwargs)

    monkeypatch.setattr(aiosqlite.Connection, "execute", fail_on_backfill)
    with pytest.raises(RuntimeError):
        await SqliteCheckRepository(temp_db)._get_notification_state_async(1)

    # The interrupted upgrade must have rolled back, so a clean retry still
    # sees the columns as missing and performs the adoption.
    monkeypatch.undo()
    monkeypatch.setattr(
        "nyxmon.adapters.repositories.sqlite_repo.current_epoch", lambda: 1_700_000_000
    )
    state = await SqliteCheckRepository(temp_db)._get_notification_state_async(1)

    assert state.last_notified_at == 1_700_000_000, (
        "an already-alerting streak was not adopted after an interrupted "
        "upgrade - it would be re-paged on rollout"
    )
    assert state.first_failure_at == 1_700_000_000
