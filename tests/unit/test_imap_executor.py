"""Unit tests for the IMAP check executor."""

from datetime import datetime, timedelta, timezone
from typing import Any

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
        messages: list[ImapMessage] | None = None,
        search_results: list[list[ImapMessage]] | None = None,
        error: Exception | None = None,
        fail_first: bool = False,
    ) -> None:
        self.messages = messages or []
        self.search_results = search_results
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

        messages = self.messages
        if self.search_results is not None:
            if not self.search_results:
                messages = []
            else:
                result_index = min(self.search_calls - 1, len(self.search_results) - 1)
                messages = self.search_results[result_index]

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        return [
            msg
            for msg in messages
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
    """Should return ERROR after retrying when no messages are recent enough."""
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
            "retries": 2,
            "retry_delay": 0,
        }
    )

    result = anyio.run(executor.execute, check)

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "no_recent_message"
    assert result.data["attempts"] == 3
    assert session.enter_count == 3
    assert session.search_calls == 3
    assert session.deleted == []


def test_no_recent_messages_can_return_warning() -> None:
    """Configured Gmail-style loopbacks can warn instead of paging critical."""
    session = StubSession(messages=[])

    executor = ImapCheckExecutor(session_factory=lambda *_: session)
    check = _build_check(
        config={
            "username": "user",
            "password": "secret",
            "search_subject": "[nyxmon]",
            "max_age_minutes": 15,
            "retries": 0,
            "no_recent_message_severity": "warning",
        }
    )

    result = anyio.run(executor.execute, check)

    assert result.status == ResultStatus.WARNING
    assert result.data["error_type"] == "no_recent_message"
    assert result.data["severity"] == "warning"
    assert result.data["attempts"] == 1


def test_no_recent_message_retries_then_succeeds_and_deletes() -> None:
    """A message delivered after the first search should be found on retry."""
    now = datetime.now(timezone.utc)
    message = ImapMessage(uid="4", subject="[nyxmon]", internaldate=now)
    session = StubSession(search_results=[[], [message]])

    executor = ImapCheckExecutor(session_factory=lambda *_: session)
    check = _build_check(
        config={
            "username": "user",
            "password": "secret",
            "search_subject": "[nyxmon]",
            "max_age_minutes": 15,
            "delete_after_check": True,
            "retries": 2,
            "retry_delay": 0,
        }
    )

    result = anyio.run(executor.execute, check)

    assert result.status == ResultStatus.OK
    assert result.data["matched_uids"] == ["4"]
    assert result.data["latest_internaldate"] == now.isoformat()
    assert session.enter_count == 2
    assert session.search_calls == 2
    assert session.deleted == ["4"]


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
