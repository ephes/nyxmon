"""Telegram transport logging tests."""

import httpx
import pytest

from nyxmon.adapters.notification import AsyncTelegramNotifier


_HTTPX_CLIENT = httpx.AsyncClient


def _client_returning(response=None, *, raises=None):
    def respond(request):
        if raises is not None:
            raise raises
        return response

    return lambda: _HTTPX_CLIENT(transport=httpx.MockTransport(respond))


@pytest.mark.anyio
async def test_http_failure_log_omits_bot_token_and_body(monkeypatch, caplog):
    token = "secret-bot-token"
    body = {"ok": False, "description": f"synthetic private response {token}"}
    monkeypatch.setattr(
        "nyxmon.adapters.notification.httpx.AsyncClient",
        _client_returning(httpx.Response(400, json=body)),
    )
    notifier = AsyncTelegramNotifier(token=token, chat_id="123")

    assert await notifier.async_send("test") is False
    assert token not in caplog.text
    assert "synthetic private response" not in caplog.text
    assert "HTTP status 400" in caplog.text


@pytest.mark.anyio
async def test_http_failure_omits_long_details(monkeypatch, caplog):
    token = "TOPSECRETVALUE"
    monkeypatch.setattr(
        "nyxmon.adapters.notification.httpx.AsyncClient",
        _client_returning(httpx.Response(400, text=("x" * 495) + token)),
    )
    notifier = AsyncTelegramNotifier(token=token, chat_id="123")
    assert await notifier.async_send("test") is False
    assert token[:5] not in caplog.text
    assert "xxxxx" not in caplog.text


@pytest.mark.anyio
async def test_async_send_reports_delivery(monkeypatch) -> None:
    monkeypatch.setattr(
        "nyxmon.adapters.notification.httpx.AsyncClient",
        _client_returning(httpx.Response(200, json={"ok": True})),
    )
    notifier = AsyncTelegramNotifier(token="t", chat_id="123")
    assert await notifier.async_send("hello") is True


@pytest.mark.anyio
async def test_async_send_reports_an_http_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "nyxmon.adapters.notification.httpx.AsyncClient",
        _client_returning(httpx.Response(500, json={"ok": True})),
    )
    notifier = AsyncTelegramNotifier(token="t", chat_id="123")
    assert await notifier.async_send("hello") is False


@pytest.mark.anyio
async def test_async_send_reports_a_transport_failure(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        "nyxmon.adapters.notification.httpx.AsyncClient",
        _client_returning(raises=httpx.ConnectError("synthetic-secret-token")),
    )
    notifier = AsyncTelegramNotifier(token="synthetic-secret-token", chat_id="123")
    assert await notifier.async_send("hello") is False
    assert "synthetic-secret-token" not in caplog.text


@pytest.mark.anyio
async def test_async_send_without_credentials_reports_no_attempt() -> None:
    """No credentials is "cannot tell", not "the send failed".

    A literal ``False`` is a delivery failure to every caller, and a failure
    keeps the incident's ``delivery_pending`` marker set, which retries every
    minute. An installation that never configured Telegram must not be pushed
    into that loop by a knob it never turned on.
    """
    notifier = AsyncTelegramNotifier(token="", chat_id="")

    assert await notifier.async_send("hello") is None


def test_notify_check_failed_without_a_portal_reports_no_attempt() -> None:
    """Same reasoning for the sync wrapper: nothing was attempted."""
    from nyxmon.domain.models import Check, CheckType, Result, ResultStatus

    notifier = AsyncTelegramNotifier(token="t", chat_id="123")
    check = Check(
        check_id=1,
        service_id=1,
        name="disk",
        check_type=CheckType.HTTP,
        url="https://example.test/health",
        data={},
    )
    result = Result(check_id=1, status=ResultStatus.ERROR, data={})

    assert notifier.notify_check_failed(check, result) is None


@pytest.mark.anyio
async def test_notify_check_failed_propagates_a_missing_credential_send(
    monkeypatch,
) -> None:
    """The ``None`` of a credential-less send survives the whole call chain."""
    from nyxmon.domain.models import Check, CheckType, Result, ResultStatus

    monkeypatch.delenv("OPSGATE_SUBMIT_BASE_URL", raising=False)
    monkeypatch.delenv("OPSGATE_SUBMIT_TOKEN", raising=False)
    notifier = AsyncTelegramNotifier(token="", chat_id="")
    check = Check(
        check_id=1,
        service_id=1,
        name="disk",
        check_type=CheckType.HTTP,
        url="https://example.test/health",
        data={},
    )
    result = Result(check_id=1, status=ResultStatus.ERROR, data={"error_msg": "full"})

    assert await notifier.async_notify_check_failed(check, result) is None


@pytest.mark.anyio
async def test_notify_check_failed_propagates_the_send_outcome(monkeypatch) -> None:
    from nyxmon.domain.models import Check, CheckType, Result, ResultStatus

    notifier = AsyncTelegramNotifier(token="t", chat_id="123")
    outcomes = iter([True, False])

    async def fake_send(message: str, high_priority: bool = False) -> bool:
        del message, high_priority
        return next(outcomes)

    monkeypatch.setattr(notifier, "async_send", fake_send)
    check = Check(
        check_id=1,
        service_id=1,
        name="disk",
        check_type=CheckType.HTTP,
        url="https://example.test/health",
        data={},
    )
    result = Result(check_id=1, status=ResultStatus.ERROR, data={"error_msg": "full"})

    assert await notifier.async_notify_check_failed(check, result) is True
    assert await notifier.async_notify_check_failed(check, result) is False


@pytest.mark.anyio
async def test_opsgate_ticket_false_opens_no_ticket_even_with_warnings_enabled(
    monkeypatch,
) -> None:
    """A recovery summary is already resolved: it must never open a ticket."""
    from nyxmon.domain.models import Check, Result, ResultStatus

    notifier = AsyncTelegramNotifier(token="t", chat_id="123")
    notifier.opsgate_include_warnings = True
    notifier.opsgate_submit_base_url = "https://opsgate.test"
    notifier.opsgate_submit_token = "secret"
    tickets: list[tuple[Check, Result]] = []
    sent: list[str] = []

    async def fake_ticket(check, result):
        tickets.append((check, result))
        return {"status": "created", "ticket_id": "abc"}

    async def fake_send(message: str, high_priority: bool = False) -> bool:
        del high_priority
        sent.append(message)
        return True

    monkeypatch.setattr(notifier, "_create_opsgate_ticket", fake_ticket)
    monkeypatch.setattr(notifier, "async_send", fake_send)
    check = Check(
        check_id=0,
        service_id=0,
        name="site connectivity",
        check_type="internal",
        url="internal://collector",
        data={},
    )

    summary = Result(
        check_id=0,
        status=ResultStatus.WARNING,
        data={"error_msg": "recovered", "opsgate_ticket": False},
    )
    await notifier.async_notify_check_failed(check, summary)
    assert tickets == []

    # An error-severity message with the same flag opens no ticket either.
    critical = Result(
        check_id=0,
        status=ResultStatus.ERROR,
        data={"error_msg": "recovered", "opsgate_ticket": False},
    )
    await notifier.async_notify_check_failed(check, critical)
    assert tickets == []

    # Without the flag the ordinary ticket path is untouched.
    ordinary = Result(
        check_id=0, status=ResultStatus.ERROR, data={"error_msg": "outage"}
    )
    await notifier.async_notify_check_failed(check, ordinary)
    assert len(tickets) == 1
    assert len(sent) == 3
    assert "OpsGate Approval Needed" in sent[2]
    assert "OpsGate" not in sent[0]
