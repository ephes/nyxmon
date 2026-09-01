"""Telegram transport logging tests."""

import httpx
import pytest

from nyxmon.adapters.notification import AsyncTelegramNotifier


@pytest.mark.anyio
async def test_http_failure_log_redacts_bot_token(monkeypatch, caplog) -> None:
    token = "secret-bot-token"
    response = httpx.Response(
        400,
        json={"ok": False, "description": f"bad token {token}"},
        request=httpx.Request(
            "POST", f"https://api.telegram.org/bot{token}/sendMessage"
        ),
    )

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, *args, **kwargs):
            return response

    monkeypatch.setattr(
        "nyxmon.adapters.notification.httpx.AsyncClient", FakeClient
    )
    notifier = AsyncTelegramNotifier(token=token, chat_id="123")

    await notifier.async_send("test")

    assert token not in caplog.text
    assert "<redacted>" in caplog.text
    assert "HTTP status 400" in caplog.text


@pytest.mark.anyio
async def test_http_failure_redacts_before_detail_truncation(monkeypatch, caplog) -> None:
    token = "TOPSECRETVALUE"
    response = httpx.Response(
        400,
        text=("x" * 495) + token,
        request=httpx.Request(
            "POST", f"https://api.telegram.org/bot{token}/sendMessage"
        ),
    )

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, *args, **kwargs):
            return response

    monkeypatch.setattr(
        "nyxmon.adapters.notification.httpx.AsyncClient", FakeClient
    )
    notifier = AsyncTelegramNotifier(token=token, chat_id="123")

    await notifier.async_send("test")

    assert token[:5] not in caplog.text
