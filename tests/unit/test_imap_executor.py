"""Unit tests for the IMAP check executor."""

from datetime import datetime, timedelta, timezone
from typing import Any, List

import anyio
from nyxmon.adapters.runner.executors.imap_executor import (
    ImapCheckExecutor,
    ImapMessage,
    ImapTransientError,
)
from nyxmon.domain import Check, ResultStatus


def _build_check(**data: Any) -> Check:
    default_config = {
        "host": "imap.example.com",
        "username": "user",
        "password": "secret",
        "search_subject": "[nyxmon]",
    }
    override_config = data.get("config", {})
    default_config.update(override_config)

    return Check(
        check_id=data.get("check_id", 1),
        service_id=1,
        name=data.get("name", "IMAP Test"),
        check_type="imap",
        url=data.get("url", "imap.example.com"),
        data=default_config,
    )


class StubSession:
    """Async stub that mimics an IMAP session."""

    def __init__(
        self,
        *,
        messages: List[ImapMessage] | None = None,
        error: Exception | None = None,
        fail_first: bool = False,
    ) -> None:
        self.messages = messages or []
        self.error = error
        self.fail_first = fail_first
        self.deleted: list[str] = []
        self.enter_count = 0
        self.search_calls = 0

    async def __aenter__(self) -> "StubSession":
        self.enter_count += 1
        if self.fail_first:
            self.fail_first = False
            raise ImapTransientError("temporary")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None

    async def search_recent(
        self, subject: str, max_age_minutes: int
    ) -> list[ImapMessage]:
        self.search_calls += 1
        if self.error:
            raise self.error

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        return [
            msg
            for msg in self.messages
            if msg.internaldate >= cutoff and subject in msg.subject
        ]

    async def delete_messages(self, ids: list[str]) -> None:
        self.deleted.extend(ids)


def test_successful_search_and_delete(monkeypatch) -> None:
    """Should return OK when a recent message is found and delete it."""
    now = datetime.now(timezone.utc)
    messages = [
        ImapMessage(
            uid="1", subject="[nyxmon] a", internaldate=now - timedelta(minutes=5)
        ),
        ImapMessage(uid="2", subject="[nyxmon] b", internaldate=now),
    ]
    session = StubSession(messages=messages)

    executor = ImapCheckExecutor(session_factory=lambda *_: session)

    check = _build_check(
        config={
            "host": "imap.example.com",
            "username": "user",
            "password": "secret",
            "search_subject": "[nyxmon]",
            "delete_after_check": True,
            "max_age_minutes": 10,
        }
    )

    result = anyio.run(executor.execute, check)

    assert session.enter_count == 1
    assert result.status == ResultStatus.OK
    assert result.data["matched_uids"] == ["1", "2"]
    assert session.deleted == ["1", "2"]


def test_no_recent_messages_returns_error() -> None:
    """Should return ERROR when no matching messages are recent enough."""
    old = datetime.now(timezone.utc) - timedelta(minutes=60)
    messages = [ImapMessage(uid="1", subject="[nyxmon]", internaldate=old)]
    session = StubSession(messages=messages)

    executor = ImapCheckExecutor(session_factory=lambda *_: session)
    check = _build_check(
        config={
            "username": "user",
            "password": "secret",
            "search_subject": "[nyxmon]",
            "max_age_minutes": 15,
        }
    )

    result = anyio.run(executor.execute, check)

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "no_recent_message"


def test_transient_error_retries_then_succeeds(monkeypatch) -> None:
    """Transient errors should be retried according to config."""
    now = datetime.now(timezone.utc)
    session = StubSession(
        messages=[ImapMessage(uid="3", subject="[nyxmon]", internaldate=now)],
        fail_first=True,
    )

    executor = ImapCheckExecutor(session_factory=lambda *_: session)

    # Make retry faster for test
    async def _fast_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "nyxmon.adapters.runner.executors.imap_executor.anyio.sleep", _fast_sleep
    )

    check = _build_check(
        config={
            "host": "imap.example.com",
            "username": "user",
            "password": "secret",
            "search_subject": "[nyxmon]",
            "retries": 1,
            "retry_delay": 0,
        }
    )

    result = anyio.run(executor.execute, check)

    assert session.search_calls == 1  # search called only after successful enter
    assert result.status == ResultStatus.OK


def test_non_retryable_error_returns_error() -> None:
    session = StubSession(error=RuntimeError("auth failed"))
    executor = ImapCheckExecutor(session_factory=lambda *_: session)

    check = _build_check(
        config={"username": "user", "password": "secret", "search_subject": "[nyxmon]"}
    )

    result = anyio.run(executor.execute, check)

    assert result.status == ResultStatus.ERROR
    assert "auth" in result.data["error_msg"]


def test_invalid_configuration_short_circuits() -> None:
    """Invalid config should fail without hitting the session."""
    session = StubSession()
    executor = ImapCheckExecutor(session_factory=lambda *_: session)

    check = _build_check(
        config={
            "host": "imap.example.com",
            "username": "user",
            "password": "secret",
            "search_subject": "",
        }
    )  # missing search_subject

    result = anyio.run(executor.execute, check)

    assert session.enter_count == 0
    assert result.status == ResultStatus.ERROR
    assert "configuration" in result.data["error_type"]
