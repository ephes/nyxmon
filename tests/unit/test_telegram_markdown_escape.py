"""MarkdownV2 escaping, alert length caps and the plain-text fallback."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from nyxmon.adapters.notification import (
    ALERT_MAX_ERROR_MSG_CHARS,
    ALERT_MAX_NAME_CHARS,
    ALERT_MAX_URL_CHARS,
    AsyncTelegramNotifier,
    markdown_v2_to_plain_text,
)
from nyxmon.domain.models import Check, CheckType, Result, ResultStatus

#: Characters Telegram requires to be escaped outside entities.
_RESERVED = set("_*[]()~`>#+-=|{}.!")


def _assert_plain_markdown_v2(text: str) -> None:
    """Assert ``text`` is valid MarkdownV2 that contains no entities at all.

    Telegram rejects a reserved character that is not preceded by a
    backslash, and a backslash must always escape the next character.
    """
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\":
            assert i + 1 < len(text), f"dangling backslash in {text!r}"
            i += 2
            continue
        assert char not in _RESERVED, f"unescaped {char!r} at {i} in {text!r}"
        i += 1


def _unescape(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\":
            out.append(text[i + 1])
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


@pytest.mark.parametrize(
    "raw",
    [
        "trailing\\",
        "C:\\path\\.",
        "double \\\\ backslash",
        "\\\\.",
        "mixed_[a](b)*c*~d~`e` > #+-=|{}.!",
        "",
    ],
)
def test_escape_markdown_v2_produces_valid_markdown_v2(raw: str) -> None:
    escaped = AsyncTelegramNotifier.escape_markdown_v2(raw)

    _assert_plain_markdown_v2(escaped)
    assert _unescape(escaped) == raw


def test_escape_markdown_v2_golden_backslash_dot() -> None:
    assert AsyncTelegramNotifier.escape_markdown_v2("C:\\path\\.") == (
        "C:\\\\path\\\\\\."
    )


def test_markdown_v2_to_plain_text_restores_the_source_text() -> None:
    raw = "C:\\path\\. a_b \\\\ end\\"
    body = (
        "🔴 *Check Failed \\(Critical\\)*\n"
        f"Name: {AsyncTelegramNotifier.escape_markdown_v2(raw)}\n"
        "[Open approval page](https://x.test/a_b\\)c)"
    )

    assert markdown_v2_to_plain_text(body) == (
        f"🔴 Check Failed (Critical)\nName: {raw}\n"
        "Open approval page (https://x.test/a_b)c)"
    )


def _check(**overrides: Any) -> Check:
    values: dict[str, Any] = {
        "check_id": 1,
        "service_id": 1,
        "name": "disk",
        "check_type": CheckType.HTTP,
        "url": "https://example.test/health",
        "data": {},
    }
    values.update(overrides)
    return Check(**values)


@pytest.mark.anyio
async def test_long_alert_fields_are_truncated(monkeypatch) -> None:
    monkeypatch.delenv("OPSGATE_SUBMIT_BASE_URL", raising=False)
    monkeypatch.delenv("OPSGATE_SUBMIT_TOKEN", raising=False)
    notifier = AsyncTelegramNotifier(token="t", chat_id="123")
    sent: list[str] = []

    async def fake_send(message: str, high_priority: bool = False) -> bool:
        del high_priority
        sent.append(message)
        return True

    monkeypatch.setattr(notifier, "async_send", fake_send)
    check = _check(name="n" * 5000, url="https://example.test/" + "u" * 5000)
    result = Result(
        check_id=1,
        status=ResultStatus.ERROR,
        data={"error_msg": "x" * 20000, "error_type": "t" * 5000},
    )

    assert await notifier.async_notify_check_failed(check, result) is True

    (message,) = sent
    assert len(_unescape(message)) < 4096
    plain = _unescape(message)
    assert "x" * ALERT_MAX_ERROR_MSG_CHARS + "...[truncated 18000 chars]" in plain
    assert "x" * (ALERT_MAX_ERROR_MSG_CHARS + 1) not in message
    assert "n" * (ALERT_MAX_NAME_CHARS + 1) not in message
    assert "u" * ALERT_MAX_URL_CHARS not in message


@pytest.mark.anyio
async def test_alert_with_backslashes_is_valid_markdown_v2(monkeypatch) -> None:
    monkeypatch.delenv("OPSGATE_SUBMIT_BASE_URL", raising=False)
    monkeypatch.delenv("OPSGATE_SUBMIT_TOKEN", raising=False)
    notifier = AsyncTelegramNotifier(token="t", chat_id="123")
    sent: list[str] = []

    async def fake_send(message: str, high_priority: bool = False) -> bool:
        del high_priority
        sent.append(message)
        return True

    monkeypatch.setattr(notifier, "async_send", fake_send)
    result = Result(
        check_id=1,
        status=ResultStatus.WARNING,
        data={
            "error_msg": "C:\\path\\. trailing\\",
            "error_type": "a\\b",
            "status_code": "5\\0.",
        },
    )

    await notifier.async_notify_check_failed(_check(name="n\\."), result)

    body = sent[0]
    # Drop the fixed bold title, which is the only entity in the message.
    title, rest = body.split("\n", 1)
    assert title == "⚠️ *Check Warning*"
    _assert_plain_markdown_v2(rest)
    assert "Error: C:\\path\\. trailing\\" in _unescape(rest)


class _Recorder:
    """Fake ``httpx.AsyncClient`` replaying a scripted list of responses."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.payloads: list[dict[str, Any]] = []

    def __call__(self) -> _Recorder:
        return self

    async def __aenter__(self) -> _Recorder:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, url: str, *, data: dict[str, Any], timeout: float):
        del timeout
        self.payloads.append(dict(data))
        response = self.responses.pop(0)
        response.request = httpx.Request("POST", url)
        return response


_PARSE_ERROR_BODY = {
    "ok": False,
    "error_code": 400,
    "description": "Bad Request: can't parse entities: Character '.' is reserved",
}


@pytest.mark.anyio
async def test_parse_error_is_resent_once_as_plain_text(monkeypatch) -> None:
    recorder = _Recorder(
        [httpx.Response(400, json=_PARSE_ERROR_BODY), httpx.Response(200, json={})]
    )
    monkeypatch.setattr("nyxmon.adapters.notification.httpx.AsyncClient", recorder)
    notifier = AsyncTelegramNotifier(token="t", chat_id="123")

    delivered = await notifier.async_send(
        "*Check Failed*\nError: C:\\\\path\\\\.", high_priority=True
    )

    assert delivered is True
    first, second = recorder.payloads
    assert first["parse_mode"] == "MarkdownV2"
    assert "parse_mode" not in second
    assert second["text"] == "Check Failed\nError: C:\\path\\."
    assert second["disable_notification"] is False


@pytest.mark.anyio
async def test_failed_plain_text_resend_reports_failure(monkeypatch) -> None:
    recorder = _Recorder(
        [httpx.Response(400, json=_PARSE_ERROR_BODY), httpx.Response(500, json={})]
    )
    monkeypatch.setattr("nyxmon.adapters.notification.httpx.AsyncClient", recorder)
    notifier = AsyncTelegramNotifier(token="t", chat_id="123")

    assert await notifier.async_send("x\\.") is False
    assert len(recorder.payloads) == 2


@pytest.mark.anyio
async def test_other_400_errors_are_not_resent(monkeypatch) -> None:
    recorder = _Recorder(
        [
            httpx.Response(
                400, json={"ok": False, "description": "Bad Request: chat not found"}
            )
        ]
    )
    monkeypatch.setattr("nyxmon.adapters.notification.httpx.AsyncClient", recorder)
    notifier = AsyncTelegramNotifier(token="t", chat_id="123")

    assert await notifier.async_send("hello") is False
    assert len(recorder.payloads) == 1
