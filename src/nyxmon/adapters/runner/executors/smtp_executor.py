"""SMTP check executor implementation."""

import datetime as dt
import secrets
import smtplib
import socket
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Protocol

import anyio

from ....domain import Check, Result, ResultStatus
from ....domain.smtp_config import SmtpCheckConfig


@dataclass
class SmtpSendResponse:
    """Structured response from SMTP client."""

    code: int
    message: str


class SmtpSendError(Exception):
    """Raised when SMTP sending fails."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        temporary: bool = False,
        error_type: str = "smtp_error",
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.temporary = temporary
        self.error_type = error_type


class SmtpClient(Protocol):
    """Protocol describing the SMTP client interface expected by the executor."""

    async def send_mail(
        self, config: SmtpCheckConfig, message: EmailMessage
    ) -> SmtpSendResponse:
        """Send a message using the given configuration."""

    async def aclose(self) -> None:
        """Optional cleanup hook for clients that manage resources."""


def _decode_smtp_response(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        try:
            return data.decode()
        except Exception:
            return repr(data)
    return str(data)


def _is_temporary(code: int | None) -> bool:
    return bool(code and 400 <= code < 500)


class SmtplibClient:
    """SMTP client backed by Python's smtplib (run in a worker thread)."""

    def __init__(self) -> None:
        self._ssl_context = ssl.create_default_context()

    async def send_mail(
        self, config: SmtpCheckConfig, message: EmailMessage
    ) -> SmtpSendResponse:
        return await anyio.to_thread.run_sync(self._send_blocking, config, message)

    def _send_blocking(
        self, config: SmtpCheckConfig, message: EmailMessage
    ) -> SmtpSendResponse:
        smtp_cls = smtplib.SMTP_SSL if config.tls == "implicit" else smtplib.SMTP

        try:
            with smtp_cls(
                config.host,
                config.port,
                timeout=config.timeout,
            ) as client:
                client.ehlo()

                if config.tls == "starttls":
                    client.starttls(context=self._ssl_context)
                    client.ehlo()

                if config.username:
                    password = config.get_password()
                    if password is None:
                        raise SmtpSendError(
                            "password required for authentication",
                            error_type="configuration_error",
                        )
                    client.login(config.username, password)

                refused = client.send_message(message)

                if refused:
                    first_code, first_response = next(iter(refused.values()))
                    code = int(first_code)
                    raise SmtpSendError(
                        _decode_smtp_response(first_response),
                        code=code,
                        temporary=_is_temporary(code),
                        error_type="recipient_refused",
                    )

                return SmtpSendResponse(code=250, message="accepted")

        except smtplib.SMTPAuthenticationError as exc:  # noqa: PERF203 - narrow mapping
            raise SmtpSendError(
                _decode_smtp_response(exc.smtp_error) or "authentication failed",
                code=exc.smtp_code,
                temporary=False,
                error_type="auth_error",
            ) from exc
        except smtplib.SMTPResponseException as exc:
            raise SmtpSendError(
                _decode_smtp_response(exc.smtp_error),
                code=exc.smtp_code,
                temporary=_is_temporary(exc.smtp_code),
                error_type="temporary_failure"
                if _is_temporary(exc.smtp_code)
                else "smtp_error",
            ) from exc
        except socket.timeout as exc:
            raise SmtpSendError(
                "connection timed out",
                error_type="timeout",
            ) from exc
        except (
            smtplib.SMTPConnectError,
            smtplib.SMTPServerDisconnected,
            ConnectionRefusedError,
        ) as exc:
            raise SmtpSendError(
                str(exc),
                error_type="connection_error",
            ) from exc
        except smtplib.SMTPException as exc:
            raise SmtpSendError(str(exc), error_type="smtp_error") from exc
        except OSError as exc:
            raise SmtpSendError(str(exc), error_type="connection_error") from exc

    async def aclose(self) -> None:
        """No-op cleanup hook for interface compatibility."""
        return None


class SmtpCheckExecutor:
    """Executor for SMTP checks with retry/backoff for greylisting."""

    def __init__(self, client: SmtpClient | None = None) -> None:
        self._client = client or SmtplibClient()

    async def execute(self, check: Check) -> Result:
        try:
            config = SmtpCheckConfig.from_dict(check.data)
            config.validate()
        except ValueError as exc:
            return Result(
                check_id=check.check_id,
                status=ResultStatus.ERROR,
                data={
                    "error_type": "configuration_error",
                    "error_msg": str(exc),
                },
            )

        subject, token = self._build_subject(config.subject_prefix)
        message = self._build_message(config, subject, token)

        for attempt in range(config.retries + 1):
            attempt_number = attempt + 1
            try:
                response = await self._client.send_mail(config, message)
                return Result(
                    check_id=check.check_id,
                    status=ResultStatus.OK,
                    data={
                        "response_code": response.code,
                        "response_message": response.message,
                        "attempts": attempt_number,
                        "subject": subject,
                        "token": token,
                        "from": config.from_addr,
                        "to": config.to_addr,
                    },
                )
            except SmtpSendError as err:
                if err.temporary and attempt < config.retries:
                    await anyio.sleep(config.retry_delay)
                    continue

                error_data = {
                    "error_type": err.error_type,
                    "error_msg": err.message,
                    "attempts": attempt_number,
                }

                if err.code is not None:
                    error_data["smtp_code"] = err.code

                return Result(
                    check_id=check.check_id,
                    status=ResultStatus.ERROR,
                    data=error_data,
                )
            except Exception as exc:  # noqa: BLE001 - bubble unexpected errors to result
                return Result(
                    check_id=check.check_id,
                    status=ResultStatus.ERROR,
                    data={
                        "error_type": "unexpected_error",
                        "error_msg": str(exc),
                        "attempts": attempt_number,
                    },
                )

        # Fallback: should never be reached
        return Result(
            check_id=check.check_id,
            status=ResultStatus.ERROR,
            data={
                "error_type": "unexpected_error",
                "error_msg": "exhausted retries without producing a result",
                "attempts": config.retries + 1,
            },
        )

    def _build_subject(self, prefix: str) -> tuple[str, str]:
        """Compose subject using prefix + UTC timestamp + 6-char token."""
        timestamp = (
            dt.datetime.now(dt.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        token = secrets.token_hex(3)
        subject = f"{prefix} {timestamp} {token}".strip()
        return subject, token

    def _build_message(
        self, config: SmtpCheckConfig, subject: str, token: str
    ) -> EmailMessage:
        message = EmailMessage()
        message["From"] = config.from_addr
        message["To"] = config.to_addr
        message["Subject"] = subject
        message["Message-ID"] = make_msgid(
            domain=self._message_id_domain(config.from_addr)
        )
        message.set_content(
            f"Nyxmon SMTP health check. Correlation token: {token}. Safe to delete."
        )
        return message

    def _message_id_domain(self, from_addr: str) -> str:
        """Derive a Message-ID domain from the sender address with a safe fallback."""
        if "@" in from_addr:
            return from_addr.split("@", 1)[1]
        return "nyxmon.local"

    async def aclose(self) -> None:
        """Cleanup the underlying SMTP client if needed."""
        if hasattr(self._client, "aclose"):
            await self._client.aclose()
