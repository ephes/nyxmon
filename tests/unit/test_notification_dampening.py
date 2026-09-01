"""Unit tests for notification dampening."""

from __future__ import annotations

from typing import Any

from nyxmon.adapters.repositories import InMemoryStore
from nyxmon.adapters.repositories.interface import (
    NotificationState,
    NotificationStateConflict,
)
from nyxmon.domain.commands import AddCheckResult
from nyxmon.domain.models import (
    Check,
    CheckResult,
    CheckStatus,
    CheckType,
    Result,
    ResultStatus,
)
from nyxmon.service_layer.handlers import add_check_result
from nyxmon.service_layer.unit_of_work import UnitOfWork


class StubNotifier:
    def __init__(self) -> None:
        self.failed_notifications: list[tuple[Check, Result]] = []

    def notify_check_failed(self, check: Check, result: Result) -> None:
        self.failed_notifications.append((check, result))

    def notify_service_status_changed(self, service: Any, status: str) -> None:
        del service, status


def _build_check() -> Check:
    return Check(
        check_id=1,
        service_id=1,
        name="HTTP",
        check_type=CheckType.HTTP,
        url="https://example.test/health",
        data={},
    )


def _add_result(
    uow: UnitOfWork,
    notifier: StubNotifier,
    status: str,
) -> Result:
    try:
        check = uow.store.checks.get(1)
    except KeyError:
        check = _build_check()
        uow.store.checks.add(check)
    result = Result(check_id=check.check_id, status=status, data={})
    add_check_result(
        AddCheckResult(check_result=CheckResult(check=check, result=result)),
        uow,
        notifier,
    )
    return result


def test_first_error_is_stored_but_not_notified(monkeypatch) -> None:
    monkeypatch.delenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", raising=False)
    uow = UnitOfWork(store=InMemoryStore())
    notifier = StubNotifier()

    _add_result(uow, notifier, ResultStatus.ERROR)

    assert len(uow.store.results.list()) == 1
    assert notifier.failed_notifications == []


def test_second_consecutive_error_notifies(monkeypatch) -> None:
    monkeypatch.delenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", raising=False)
    uow = UnitOfWork(store=InMemoryStore())
    notifier = StubNotifier()

    _add_result(uow, notifier, ResultStatus.ERROR)
    _add_result(uow, notifier, ResultStatus.ERROR)

    assert len(uow.store.results.list()) == 2
    assert len(notifier.failed_notifications) == 1


def test_continued_failures_do_not_notify_every_interval(monkeypatch) -> None:
    monkeypatch.delenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", raising=False)
    uow = UnitOfWork(store=InMemoryStore())
    notifier = StubNotifier()

    _add_result(uow, notifier, ResultStatus.ERROR)
    _add_result(uow, notifier, ResultStatus.ERROR)
    _add_result(uow, notifier, ResultStatus.ERROR)

    assert len(uow.store.results.list()) == 3
    assert len(notifier.failed_notifications) == 1


def test_ok_between_errors_resets_sequence(monkeypatch) -> None:
    monkeypatch.delenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", raising=False)
    uow = UnitOfWork(store=InMemoryStore())
    notifier = StubNotifier()

    _add_result(uow, notifier, ResultStatus.ERROR)
    _add_result(uow, notifier, ResultStatus.OK)
    _add_result(uow, notifier, ResultStatus.ERROR)

    assert len(uow.store.results.list()) == 3
    assert notifier.failed_notifications == []


def test_threshold_can_be_configured(monkeypatch) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "3")
    uow = UnitOfWork(store=InMemoryStore())
    notifier = StubNotifier()

    _add_result(uow, notifier, ResultStatus.ERROR)
    _add_result(uow, notifier, ResultStatus.ERROR)
    assert notifier.failed_notifications == []

    _add_result(uow, notifier, ResultStatus.WARNING)

    assert len(notifier.failed_notifications) == 1


def test_threshold_one_notifies_first_failure(monkeypatch) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    uow = UnitOfWork(store=InMemoryStore())
    notifier = StubNotifier()

    _add_result(uow, notifier, ResultStatus.ERROR)

    assert len(notifier.failed_notifications) == 1


def test_persistent_failure_sends_periodic_reminder(monkeypatch) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "2")
    monkeypatch.setenv("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "900")
    now = 10_000
    monkeypatch.setattr("nyxmon.service_layer.handlers.current_epoch", lambda: now)
    uow = UnitOfWork(store=InMemoryStore())
    notifier = StubNotifier()

    # Five samples 300s apart: the initial alert on sample two, one elapsed-time
    # reminder once 900s have passed since it.
    for _ in range(5):
        _add_result(uow, notifier, ResultStatus.ERROR)
        now += 300

    assert len(notifier.failed_notifications) == 2
    assert len(uow.store.results.list()) == 5


def test_reminder_window_is_measured_from_the_last_notification(monkeypatch) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "5")
    monkeypatch.setenv("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "600")
    now = 10_000
    monkeypatch.setattr("nyxmon.service_layer.handlers.current_epoch", lambda: now)
    uow = UnitOfWork(store=InMemoryStore())
    notifier = StubNotifier()

    # Four samples in the same second cannot reach the threshold of five.
    for _ in range(4):
        _add_result(uow, notifier, ResultStatus.ERROR)
    assert notifier.failed_notifications == []

    _add_result(uow, notifier, ResultStatus.ERROR)
    assert len(notifier.failed_notifications) == 1

    now += 599
    _add_result(uow, notifier, ResultStatus.ERROR)
    assert len(notifier.failed_notifications) == 1

    now += 1
    _add_result(uow, notifier, ResultStatus.ERROR)
    assert len(notifier.failed_notifications) == 2


def test_immediate_failure_bypasses_streak_threshold_but_has_cooldown(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "5")
    monkeypatch.setenv("NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS", "3600")
    now = 10_000
    monkeypatch.setattr("nyxmon.service_layer.handlers.current_epoch", lambda: now)
    store = InMemoryStore()
    uow = UnitOfWork(store=store)
    notifier = StubNotifier()
    check = _build_check()
    check.disabled = True
    store.checks.add(check)
    result = Result(
        check_id=check.check_id,
        status=ResultStatus.ERROR,
        data={"notification_immediate": True},
    )

    add_check_result(
        AddCheckResult(check_result=CheckResult(check=check, result=result)),
        uow,
        notifier,
    )

    assert len(notifier.failed_notifications) == 1
    assert "notification_attempted_at_epoch" not in result.data
    assert "_nyxmon_notification_state" not in check.data

    second = Result(
        check_id=check.check_id,
        status=ResultStatus.ERROR,
        data={"notification_immediate": True},
    )
    add_check_result(
        AddCheckResult(check_result=CheckResult(check=check, result=second)),
        uow,
        notifier,
    )
    assert len(notifier.failed_notifications) == 1

    now += 3600
    third = Result(
        check_id=check.check_id,
        status=ResultStatus.ERROR,
        data={"notification_immediate": True},
    )
    add_check_result(
        AddCheckResult(check_result=CheckResult(check=check, result=third)),
        uow,
        notifier,
    )
    assert len(notifier.failed_notifications) == 2


def test_forced_internal_reminder_bypasses_cooldown_and_suppression(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS", "7200")
    monkeypatch.setattr("nyxmon.service_layer.handlers.current_epoch", lambda: 10_000)
    store = InMemoryStore()
    uow = UnitOfWork(store=store)
    notifier = StubNotifier()
    check = _build_check()
    check.disabled = True
    store.checks.add(check)

    monkeypatch.setattr(
        "nyxmon.service_layer.handlers.notification_suppression_details",
        lambda check: {"reason": "maintenance"},
    )
    for force_notification in (False, True):
        result = Result(
            check_id=check.check_id,
            status=ResultStatus.ERROR,
            data={"notification_immediate": True},
        )
        attempted = add_check_result(
            AddCheckResult(
                check_result=CheckResult(
                    check=check,
                    result=result,
                    force_notification=force_notification,
                )
            ),
            uow,
            notifier,
        )
        assert attempted is force_notification

    assert len(notifier.failed_notifications) == 1
    assert store.checks.get_notification_state(check.check_id) == NotificationState()


def test_in_memory_store_drops_result_after_check_deletion() -> None:
    store = InMemoryStore()
    check = _build_check()
    store.checks.add(check)
    del store.checks.checks[check.check_id]

    store.persist_check_result(
        check,
        Result(check_id=check.check_id, status=ResultStatus.ERROR, data={}),
        None,
    )

    assert store.checks.list() == []
    assert store.results.list() == []


def test_deleted_check_does_not_emit_unpersisted_notification(monkeypatch) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    store = InMemoryStore()
    uow = UnitOfWork(store=store)
    notifier = StubNotifier()
    check = _build_check()

    attempted = add_check_result(
        AddCheckResult(
            check_result=CheckResult(
                check=check,
                result=Result(
                    check_id=check.check_id,
                    status=ResultStatus.ERROR,
                    data={},
                ),
            )
        ),
        uow,
        notifier,
    )

    assert attempted is False
    assert notifier.failed_notifications == []


def test_in_memory_completion_preserves_concurrent_check_edits() -> None:
    store = InMemoryStore()
    current = _build_check()
    current.disabled = True
    current.name = "renamed"
    current.data = {"timeout": 99}
    store.checks.add(current)
    snapshot = _build_check()
    snapshot.schedule_next_check()

    assert store.persist_check_result(
        snapshot,
        Result(check_id=1, status=ResultStatus.OK, data={}),
        None,
    )

    persisted = store.checks.get(1)
    assert persisted.disabled is True
    assert persisted.name == "renamed"
    assert persisted.data == {"timeout": 99}


def test_superseded_in_memory_result_cannot_alert_or_change_state(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    store = InMemoryStore()
    current = _build_check()
    current.status = CheckStatus.PROCESSING
    current.processing_started_at = 200
    current.claim_started_at = 200
    store.checks.add(current)
    store.checks.set_notification_state(1, NotificationState(4, 2, 123))
    snapshot = _build_check()
    snapshot.status = CheckStatus.PROCESSING
    snapshot.processing_started_at = 100
    snapshot.claim_started_at = 100
    snapshot.schedule_next_check()
    notifier = StubNotifier()

    attempted = add_check_result(
        AddCheckResult(
            check_result=CheckResult(
                check=snapshot,
                result=Result(check_id=1, status=ResultStatus.ERROR, data={}),
            )
        ),
        UnitOfWork(store=store),
        notifier,
    )

    assert attempted is False
    assert store.checks.get_notification_state(1) == NotificationState(4, 2, 123)
    assert len(store.results.list()) == 1
    assert notifier.failed_notifications == []


def test_notification_state_survives_result_history_pruning(monkeypatch) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "2")
    monkeypatch.setenv("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "900")
    now = 10_000
    monkeypatch.setattr("nyxmon.service_layer.handlers.current_epoch", lambda: now)
    uow = UnitOfWork(store=InMemoryStore())
    notifier = StubNotifier()

    _add_result(uow, notifier, ResultStatus.ERROR)
    _add_result(uow, notifier, ResultStatus.ERROR)
    assert len(notifier.failed_notifications) == 1

    # Result retention must not erase the persisted per-check reminder state.
    uow.store.results.results.clear()
    uow.store.results._timestamps.clear()
    now += 300
    _add_result(uow, notifier, ResultStatus.ERROR)
    now += 300
    _add_result(uow, notifier, ResultStatus.ERROR)
    assert len(notifier.failed_notifications) == 1
    now += 300
    _add_result(uow, notifier, ResultStatus.ERROR)
    assert len(notifier.failed_notifications) == 2


def test_recovery_resets_immediate_alert_cooldown(monkeypatch) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS", "3600")
    now = 10_000
    monkeypatch.setattr("nyxmon.service_layer.handlers.current_epoch", lambda: now)
    store = InMemoryStore()
    uow = UnitOfWork(store=store)
    notifier = StubNotifier()
    check = _build_check()
    store.checks.add(check)

    for status, immediate in [
        (ResultStatus.ERROR, True),
        (ResultStatus.OK, False),
        (ResultStatus.ERROR, True),
    ]:
        result = Result(
            check_id=check.check_id,
            status=status,
            data={"notification_immediate": True} if immediate else {},
        )
        add_check_result(
            AddCheckResult(check_result=CheckResult(check=check, result=result)),
            uow,
            notifier,
        )

    assert len(notifier.failed_notifications) == 2


class _ConflictingStore(InMemoryStore):
    def __init__(self, conflicts: int) -> None:
        super().__init__()
        self.conflicts = conflicts
        self.persist_attempts = 0

    def persist_check_result(
        self,
        check,
        result,
        notification_transition,
        *,
        complete_check=True,
    ) -> bool:
        self.persist_attempts += 1
        if notification_transition is not None and self.conflicts > 0:
            self.conflicts -= 1
            raise NotificationStateConflict(check.check_id)
        return super().persist_check_result(
            check,
            result,
            notification_transition,
            complete_check=complete_check,
        )


def test_notification_state_conflict_retries_then_persists(monkeypatch) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "2")
    store = _ConflictingStore(conflicts=1)
    uow = UnitOfWork(store=store)
    notifier = StubNotifier()

    _add_result(uow, notifier, ResultStatus.ERROR)

    assert store.persist_attempts == 2
    assert len(store.results.list()) == 1


def test_notification_state_contention_never_discards_result(monkeypatch) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    store = _ConflictingStore(conflicts=3)
    uow = UnitOfWork(store=store)
    notifier = StubNotifier()

    _add_result(uow, notifier, ResultStatus.ERROR)

    assert store.persist_attempts == 4
    assert len(store.results.list()) == 1
    assert notifier.failed_notifications == []
