"""Unit tests for notification dampening."""

from __future__ import annotations

from typing import Any

from nyxmon.adapters.repositories import InMemoryStore
from nyxmon.domain.commands import AddCheckResult
from nyxmon.domain.models import Check, CheckResult, CheckType, Result, ResultStatus
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
    check = _build_check()
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
