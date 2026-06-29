"""Unit tests for notification suppression windows."""

from __future__ import annotations

from typing import Any

from nyxmon.adapters.repositories import InMemoryStore
from nyxmon.domain.commands import AddCheckResult
from nyxmon.domain.models import Check, CheckResult, CheckType, Result, ResultStatus
from nyxmon.service_layer import handlers
from nyxmon.service_layer.notification_suppression import notification_suppression_details
from nyxmon.service_layer.unit_of_work import UnitOfWork


class StubNotifier:
    def __init__(self) -> None:
        self.failed_notifications: list[tuple[Check, Result]] = []

    def notify_check_failed(self, check: Check, result: Result) -> None:
        self.failed_notifications.append((check, result))

    def notify_service_status_changed(self, service: Any, status: str) -> None:
        del service, status


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeHttpClient:
    def __init__(self, payload: dict[str, Any] | None = None, exc: Exception | None = None) -> None:
        self.payload = payload or {}
        self.exc = exc
        self.requests: list[dict[str, Any]] = []

    def __enter__(self) -> "FakeHttpClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        del exc_type, exc_val, exc_tb

    def get(self, url: str, *, timeout: float, auth: Any) -> FakeResponse:
        self.requests.append({"url": url, "timeout": timeout, "auth": auth})
        if self.exc:
            raise self.exc
        return FakeResponse(self.payload)


def _build_check(data: dict[str, Any]) -> Check:
    return Check(
        check_id=1,
        service_id=1,
        name="JSON metrics",
        check_type=CheckType.JSON_METRICS,
        url="https://example.test/metrics",
        data=data,
    )


def _suppression_config() -> dict[str, Any]:
    return {
        "notification_suppression": {
            "url": "https://example.test/maintenance",
            "timeout": 1.5,
            "reason": "scheduled_maintenance",
            "status_path": "$.last_status",
            "active_statuses": ["running"],
            "finished_epoch_path": "$.last_run_finished_epoch",
            "active_for_seconds": 900,
            "auth": {"username": "nyxmon", "password": "secret"},
        }
    }


def _patch_client(monkeypatch, fake_client: FakeHttpClient) -> FakeHttpClient:
    monkeypatch.setattr(
        "nyxmon.service_layer.notification_suppression.httpx.Client",
        lambda **kwargs: fake_client,
    )
    return fake_client


def test_suppression_active_while_maintenance_is_running(monkeypatch) -> None:
    client = _patch_client(monkeypatch, FakeHttpClient({"last_status": "running"}))
    check = _build_check(_suppression_config())

    details = notification_suppression_details(check, now_epoch=1_000)

    assert details == {
        "reason": "scheduled_maintenance",
        "source_url": "https://example.test/maintenance",
        "source_status": "running",
    }
    assert client.requests[0]["url"] == "https://example.test/maintenance"
    assert client.requests[0]["timeout"] == 1.5
    assert client.requests[0]["auth"] is not None


def test_suppression_active_during_recently_finished_window(monkeypatch) -> None:
    _patch_client(
        monkeypatch,
        FakeHttpClient({"last_status": "success", "last_run_finished_epoch": 900}),
    )
    check = _build_check(_suppression_config())

    details = notification_suppression_details(check, now_epoch=1_000)

    assert details == {
        "reason": "scheduled_maintenance",
        "source_url": "https://example.test/maintenance",
        "source_status": "success",
        "finished_epoch": 900,
        "active_for_seconds": 900,
    }


def test_suppression_inactive_after_recently_finished_window(monkeypatch) -> None:
    _patch_client(
        monkeypatch,
        FakeHttpClient({"last_status": "success", "last_run_finished_epoch": 99}),
    )
    check = _build_check(_suppression_config())

    assert notification_suppression_details(check, now_epoch=1_000) is None


def test_suppression_inactive_when_endpoint_errors(monkeypatch) -> None:
    _patch_client(monkeypatch, FakeHttpClient(exc=RuntimeError("offline")))
    check = _build_check(_suppression_config())

    assert notification_suppression_details(check, now_epoch=1_000) is None


def test_suppression_ignores_malformed_active_if(monkeypatch) -> None:
    _patch_client(monkeypatch, FakeHttpClient({"last_status": "success"}))
    config = _suppression_config()
    config["notification_suppression"]["active_if"] = None
    config["notification_suppression"]["active_for_seconds"] = 0
    check = _build_check(config)

    assert notification_suppression_details(check, now_epoch=1_000) is None


def test_suppression_clamps_timeout(monkeypatch) -> None:
    client = _patch_client(monkeypatch, FakeHttpClient({"last_status": "running"}))
    config = _suppression_config()
    config["notification_suppression"]["timeout"] = 999
    check = _build_check(config)

    assert notification_suppression_details(check, now_epoch=1_000) is not None
    assert client.requests[0]["timeout"] == 30.0


def test_suppressed_result_is_persisted_without_notification(monkeypatch) -> None:
    monkeypatch.delenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", raising=False)
    monkeypatch.setattr(
        handlers,
        "notification_suppression_details",
        lambda check: {"reason": "maintenance"},
    )
    uow = UnitOfWork(store=InMemoryStore())
    notifier = StubNotifier()
    check = _build_check({})
    result = Result(check_id=check.check_id, status=ResultStatus.ERROR, data={})

    handlers.add_check_result(
        AddCheckResult(check_result=CheckResult(check=check, result=result)),
        uow,
        notifier,
    )

    assert uow.store.results.list()[0].data["notification_suppressed"] == {
        "reason": "maintenance"
    }
    assert notifier.failed_notifications == []


def test_suppressed_result_breaks_failure_notification_streak(monkeypatch) -> None:
    monkeypatch.delenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", raising=False)
    suppression_calls = iter([{"reason": "maintenance"}, None, None])
    monkeypatch.setattr(
        handlers,
        "notification_suppression_details",
        lambda check: next(suppression_calls),
    )
    uow = UnitOfWork(store=InMemoryStore())
    notifier = StubNotifier()
    check = _build_check({})

    for _ in range(3):
        result = Result(check_id=check.check_id, status=ResultStatus.ERROR, data={})
        handlers.add_check_result(
            AddCheckResult(check_result=CheckResult(check=check, result=result)),
            uow,
            notifier,
        )

    assert len(uow.store.results.list()) == 3
    assert len(notifier.failed_notifications) == 1
    notified_result = notifier.failed_notifications[0][1]
    assert notified_result.result_id == 2
