from __future__ import annotations

from typing import Any

import pytest

import nyxmon.adapters.notification as notification_module
from nyxmon.adapters.notification import AsyncTelegramNotifier
from nyxmon.domain.models import Check, Result


class _FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeAsyncClient:
    def __init__(self, *, response: _FakeResponse, captured: dict[str, Any]) -> None:
        self._response = response
        self._captured = captured

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        del exc_type, exc, tb
        return False

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        self._captured["url"] = url
        self._captured["headers"] = headers or {}
        self._captured["json"] = json or {}
        self._captured["timeout"] = timeout
        return self._response


def _build_check() -> Check:
    return Check(
        check_id=11,
        service_id=22,
        name="Disk Pressure",
        check_type="json-http",
        url="https://example.internal/health",
        data={},
    )


def _build_result(status: str = "error") -> Result:
    return Result(
        check_id=11,
        status=status,
        data={"error_msg": "Disk full", "error_type": "threshold", "status_code": 500},
    )


@pytest.mark.anyio
async def test_create_opsgate_ticket_posts_expected_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSGATE_SUBMIT_BASE_URL", "https://opsgate.home")
    monkeypatch.setenv("OPSGATE_SUBMIT_TOKEN", "token-12345678901234567890")
    monkeypatch.delenv("OPSGATE_APPROVAL_BASE_URL", raising=False)

    notifier = AsyncTelegramNotifier(token="telegram-token", chat_id="123")

    captured: dict[str, Any] = {}
    fake_response = _FakeResponse(201, {"id": "aaaaaaaa-1111-4111-8111-111111111111"})

    def _client_factory() -> _FakeAsyncClient:
        return _FakeAsyncClient(response=fake_response, captured=captured)

    monkeypatch.setattr(notification_module.httpx, "AsyncClient", _client_factory)

    created = await notifier._create_opsgate_ticket(_build_check(), _build_result())

    assert created == {"status": "created", "ticket_id": "aaaaaaaa-1111-4111-8111-111111111111"}
    assert captured["url"] == "https://opsgate.home/api/v1/tickets"
    assert captured["headers"]["Authorization"] == "Bearer token-12345678901234567890"
    assert captured["json"]["task_ref"] == "nyxmon-check-11"
    assert captured["json"]["execution_plan"][0]["role"] == "investigator"


@pytest.mark.anyio
async def test_create_opsgate_ticket_disabled_without_submit_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPSGATE_SUBMIT_BASE_URL", raising=False)
    monkeypatch.delenv("OPSGATE_SUBMIT_TOKEN", raising=False)
    notifier = AsyncTelegramNotifier(token="telegram-token", chat_id="123")

    created = await notifier._create_opsgate_ticket(_build_check(), _build_result())
    assert created is None


@pytest.mark.anyio
async def test_notify_check_failed_includes_approval_link_when_ticket_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPSGATE_SUBMIT_BASE_URL", "https://opsgate.home")
    monkeypatch.setenv("OPSGATE_SUBMIT_TOKEN", "token-12345678901234567890")
    notifier = AsyncTelegramNotifier(token="telegram-token", chat_id="123")

    async def _fake_create_ticket(_check: Check, _result: Result) -> dict[str, str]:
        return {"status": "created", "ticket_id": "bbbbbbbb-2222-4222-8222-222222222222"}

    sent: dict[str, Any] = {}

    async def _fake_send(text: str, high_priority: bool = False) -> None:
        sent["text"] = text
        sent["high_priority"] = high_priority

    monkeypatch.setattr(notifier, "_create_opsgate_ticket", _fake_create_ticket)
    monkeypatch.setattr(notifier, "async_send", _fake_send)

    await notifier.async_notify_check_failed(_build_check(), _build_result("error"))

    assert sent["high_priority"] is True
    assert "OpsGate Approval Needed" in sent["text"]
    assert "Open approval page" in sent["text"]
    assert "https://opsgate.home/tickets/bbbbbbbb-2222-4222-8222-222222222222" in sent["text"]


@pytest.mark.anyio
async def test_notify_check_failed_includes_duplicate_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSGATE_SUBMIT_BASE_URL", "https://opsgate.home")
    monkeypatch.setenv("OPSGATE_SUBMIT_TOKEN", "token-12345678901234567890")
    notifier = AsyncTelegramNotifier(token="telegram-token", chat_id="123")

    async def _fake_create_ticket(_check: Check, _result: Result) -> dict[str, str]:
        return {"status": "duplicate"}

    sent: dict[str, Any] = {}

    async def _fake_send(text: str, high_priority: bool = False) -> None:
        sent["text"] = text
        sent["high_priority"] = high_priority

    monkeypatch.setattr(notifier, "_create_opsgate_ticket", _fake_create_ticket)
    monkeypatch.setattr(notifier, "async_send", _fake_send)

    await notifier.async_notify_check_failed(_build_check(), _build_result("error"))

    assert sent["high_priority"] is True
    assert "open remediation ticket already exists" in sent["text"]


@pytest.mark.anyio
async def test_notify_check_failed_includes_ticket_error_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSGATE_SUBMIT_BASE_URL", "https://opsgate.home")
    monkeypatch.setenv("OPSGATE_SUBMIT_TOKEN", "token-12345678901234567890")
    notifier = AsyncTelegramNotifier(token="telegram-token", chat_id="123")

    async def _fake_create_ticket(_check: Check, _result: Result) -> dict[str, str]:
        return {"status": "error"}

    sent: dict[str, Any] = {}

    async def _fake_send(text: str, high_priority: bool = False) -> None:
        sent["text"] = text
        sent["high_priority"] = high_priority

    monkeypatch.setattr(notifier, "_create_opsgate_ticket", _fake_create_ticket)
    monkeypatch.setattr(notifier, "async_send", _fake_send)

    await notifier.async_notify_check_failed(_build_check(), _build_result("error"))

    assert sent["high_priority"] is True
    assert "Ticket creation failed" in sent["text"]
