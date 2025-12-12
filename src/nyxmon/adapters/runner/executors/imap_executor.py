"""IMAP check executor implementation."""

from __future__ import annotations

import time
import imaplib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Protocol

import anyio

from ....domain import Check, Result, ResultStatus
from ....domain.imap_config import ImapCheckConfig
from urllib.parse import urlparse


@dataclass
class ImapMessage:
    """Representation of a matched IMAP message."""

    uid: str
    subject: str
    internaldate: datetime


class ImapCheckError(Exception):
    """Base error for IMAP check execution."""  # pragma: no cover - base class only


class ImapTransientError(ImapCheckError):
    """Errors that can be retried."""


class ImapSession(Protocol):
    """Protocol describing the IMAP session interface used by the executor."""

    async def __aenter__(self) -> "ImapSession": ...

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        ...

    async def search_recent(
        self, subject: str, max_age_minutes: int
    ) -> list[ImapMessage]: ...

    async def delete_messages(self, ids: list[str]) -> None: ...


class ImapLibSession:
    """Concrete IMAP session backed by imaplib, run in worker threads."""

    def __init__(self, host: str, config: ImapCheckConfig) -> None:
        self.host = host
        self.config = config
        self._conn: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None

    async def __aenter__(self) -> "ImapLibSession":
        await self._connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        await self._logout()

    async def _connect(self) -> None:
        def _make_connection():
            if self.config.tls_mode == "implicit":
                return imaplib.IMAP4_SSL(
                    self.host, self.config.port, timeout=self.config.timeout
                )

            conn = imaplib.IMAP4(
                self.host, self.config.port, timeout=self.config.timeout
            )
            if self.config.tls_mode == "starttls":
                typ, _ = conn.starttls()
                if typ != "OK":
                    raise ImapTransientError("STARTTLS failed")
            return conn

        self._conn = await anyio.to_thread.run_sync(_make_connection)

        await self._run_checked("login", self.config.username, self.config.password)
        await self._run_checked("select", self.config.folder)

    async def _logout(self) -> None:
        if not self._conn:
            return

        try:
            await anyio.to_thread.run_sync(self._conn.logout)
        except Exception:
            # Best-effort logout; ignore errors
            pass
        finally:
            self._conn = None

    async def _run_checked(self, method: str, *args):
        if self._conn is None:
            raise ImapCheckError("IMAP connection not initialized")

        def _call():
            fn = getattr(self._conn, method)
            return fn(*args)

        typ, data = await anyio.to_thread.run_sync(_call)
        if typ != "OK":
            raise ImapCheckError(f"{method} failed: {typ} {data}")
        return data

    async def search_recent(
        self, subject: str, max_age_minutes: int
    ) -> list[ImapMessage]:
        if self._conn is None:
            raise ImapCheckError("IMAP connection not initialized")

        search_data = await self._run_checked(
            "search", None, "NOT", "DELETED", "HEADER", "SUBJECT", f'"{subject}"'
        )
        ids = search_data[0].decode().split() if search_data and search_data[0] else []
        if not ids:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        messages: list[ImapMessage] = []

        for msg_id in ids:
            internaldate = await self._fetch_internaldate(msg_id)
            if internaldate is None or internaldate < cutoff:
                continue

            messages.append(
                ImapMessage(uid=msg_id, subject=subject, internaldate=internaldate)
            )

        return messages

    async def _fetch_internaldate(self, msg_id: str) -> datetime | None:
        data = await self._run_checked("fetch", msg_id, "(INTERNALDATE)")
        # imaplib returns list where first element contains INTERNALDATE string
        if not data or not data[0]:
            return None

        raw = data[0]
        if isinstance(raw, tuple):
            raw = raw[0]

        parsed_tuple: time.struct_time | None = None
        parsed_dt: datetime | None = None

        try:
            parsed_tuple = imaplib.Internaldate2tuple(raw)
        except Exception:
            parsed_dt = parsedate_to_datetime(
                raw.decode() if isinstance(raw, bytes) else str(raw)
            )

        if parsed_dt is not None:
            return (
                parsed_dt
                if parsed_dt.tzinfo
                else parsed_dt.replace(tzinfo=timezone.utc)
            )

        if parsed_tuple is None:
            return None

        return datetime.fromtimestamp(time.mktime(parsed_tuple), tz=timezone.utc)

    async def delete_messages(self, ids: list[str]) -> None:
        if not ids or self._conn is None:
            return

        await self._run_checked("store", ",".join(ids), "+FLAGS", "\\Deleted")
        await self._run_checked("expunge")


class ImapCheckExecutor:
    """Executor for IMAP checks."""

    def __init__(
        self,
        session_factory: Callable[[str, ImapCheckConfig], ImapSession] | None = None,
    ) -> None:
        self._session_factory = session_factory or ImapLibSession

    async def execute(self, check: Check) -> Result:
        try:
            config = ImapCheckConfig.from_dict(check.data)
            config.validate()
        except ValueError as err:
            return Result(
                check_id=check.check_id,
                status=ResultStatus.ERROR,
                data={
                    "error_type": "configuration_error",
                    "error_msg": str(err),
                },
            )

        attempts = config.retries + 1
        for attempt in range(attempts):
            try:
                return await self._run_once(check, config)
            except ImapTransientError as err:
                if attempt < config.retries:
                    await anyio.sleep(config.retry_delay)
                    continue
                return self._error_result(
                    check.check_id, "transient_failure", str(err), attempt + 1
                )
            except ImapCheckError as err:
                return self._error_result(
                    check.check_id, "execution_error", str(err), attempt + 1
                )
            except Exception as err:  # noqa: BLE001
                return self._error_result(
                    check.check_id, "unexpected_error", str(err), attempt + 1
                )

        return self._error_result(
            check.check_id, "unexpected_error", "execution failed", attempts
        )

    async def _run_once(self, check: Check, config: ImapCheckConfig) -> Result:
        host = self._normalize_host(config.host or check.url)
        session = self._session_factory(host, config)
        async with session:
            messages = await session.search_recent(
                config.search_subject, config.max_age_minutes
            )

            if not messages:
                return self._error_result(
                    check.check_id,
                    "no_recent_message",
                    f"No messages with subject '{config.search_subject}' within {config.max_age_minutes} minutes",
                    1,
                )

            # Sort to ensure deterministic latest selection
            messages.sort(key=lambda m: m.internaldate)
            matched_uids = [m.uid for m in messages]
            latest = messages[-1]

            if config.delete_after_check:
                await session.delete_messages(matched_uids)

            return Result(
                check_id=check.check_id,
                status=ResultStatus.OK,
                data={
                    "matched_uids": matched_uids,
                    "latest_internaldate": latest.internaldate.isoformat(),
                },
            )

    def _error_result(
        self, check_id: int, error_type: str, error_msg: str, attempts: int
    ) -> Result:
        return Result(
            check_id=check_id,
            status=ResultStatus.ERROR,
            data={
                "error_type": error_type,
                "error_msg": error_msg,
                "attempts": attempts,
            },
        )

    async def aclose(self) -> None:
        """Executor cleanup hook (no resources to release)."""
        return None

    def _normalize_host(self, host: str) -> str:
        """Strip schemes if a full URL was stored."""
        if "://" in host:
            parsed = urlparse(host)
            return parsed.hostname or host
        return host
