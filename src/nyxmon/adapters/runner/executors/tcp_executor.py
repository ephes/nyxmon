"""TCP check executor with TLS and certificate expiry support."""

from __future__ import annotations

import ssl
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Union, cast
from urllib.parse import urlparse

import anyio
from anyio.abc import SocketStream
from anyio.streams.tls import TLSAttribute, TLSStream

from ....domain import Check, Result, ResultStatus
from ....domain.tcp_config import TcpCheckConfig


@dataclass
class TcpCheckError(Exception):
    """Structured error for TCP executor failures."""

    error_type: str
    message: str
    retryable: bool = False
    data: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        super().__init__(self.message)
        self.args = (self.message,)

    def __str__(self) -> str:  # pragma: no cover - simple wrapper
        return self.message


class TcpCheckExecutor:
    """Executor for TCP checks including TLS handshake and cert expiry validation."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic

    async def execute(self, check: Check) -> Result:
        """Execute a TCP check and return a Result."""
        try:
            config = TcpCheckConfig.from_dict(check.data)
            config.validate()
        except ValueError as exc:
            return self._error(
                check.check_id,
                "configuration_error",
                str(exc),
                {},
            )

        host = self._resolve_host(config, check.url)
        if not host:
            return self._error(
                check.check_id,
                "configuration_error",
                "host is required (set check.url or data.host)",
                {},
            )

        attempts = config.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                data = await self._attempt_once(host, config)
                data.update({"attempt": attempt, "attempts": attempts})
                return Result(
                    check_id=check.check_id, status=ResultStatus.OK, data=data
                )
            except TcpCheckError as exc:
                error_data = {
                    "error_type": exc.error_type,
                    "error_msg": exc.message,
                    "host": host,
                    "port": config.port,
                    "tls_mode": config.tls_mode,
                    "attempt": attempt,
                    "attempts": attempts,
                }
                if exc.data:
                    error_data.update(exc.data)

                if exc.retryable and attempt < attempts:
                    await anyio.sleep(config.retry_delay)
                    continue

                return Result(
                    check_id=check.check_id,
                    status=ResultStatus.ERROR,
                    data=error_data,
                )
            except Exception as exc:  # noqa: BLE001 - capture unexpected failures
                return self._error(
                    check.check_id,
                    "unexpected_error",
                    str(exc),
                    {
                        "host": host,
                        "port": config.port,
                        "tls_mode": config.tls_mode,
                        "attempt": attempt,
                        "attempts": attempts,
                    },
                )

        # Should never reach here because loop returns on success or final failure
        return self._error(
            check.check_id,
            "unknown_error",
            "unknown failure while executing TCP check",
            {"host": host, "port": config.port, "tls_mode": config.tls_mode},
        )

    async def _attempt_once(self, host: str, config: TcpCheckConfig) -> dict[str, Any]:
        """Perform a single connection attempt including TLS negotiation."""
        base_stream, connect_time_ms = await self._connect(host, config)
        current_stream: Union[SocketStream, TLSStream] = base_stream

        tls_handshake_ms: int | None = None
        cert_days_remaining: int | None = None
        peer_cert: Any = None

        try:
            if config.tls_mode != "none":
                tls_stream, tls_handshake_ms, peer_cert = await self._negotiate_tls(
                    base_stream, host, config
                )
                current_stream = tls_stream

                if config.check_cert_expiry:
                    cert_days_remaining = self._cert_days_remaining(peer_cert)
                    if cert_days_remaining is None:
                        raise TcpCheckError(
                            "cert_missing",
                            "TLS certificate not available from peer",
                            retryable=False,
                            data={
                                "connect_time_ms": connect_time_ms,
                                "tls_handshake_ms": tls_handshake_ms,
                            },
                        )

                    if cert_days_remaining < config.min_cert_days:
                        raise TcpCheckError(
                            "cert_expiry",
                            f"Certificate expires in {cert_days_remaining} days",
                            retryable=False,
                            data={
                                "severity": "warning",
                                "cert_days_remaining": cert_days_remaining,
                                "connect_time_ms": connect_time_ms,
                                "tls_handshake_ms": tls_handshake_ms,
                            },
                        )

            return {
                "host": host,
                "port": config.port,
                "tls_mode": config.tls_mode,
                "connect_time_ms": connect_time_ms,
                **(
                    {"tls_handshake_ms": tls_handshake_ms}
                    if tls_handshake_ms is not None
                    else {}
                ),
                **(
                    {"cert_days_remaining": cert_days_remaining}
                    if cert_days_remaining is not None
                    else {}
                ),
            }
        finally:
            await current_stream.aclose()

    async def _connect(
        self, host: str, config: TcpCheckConfig
    ) -> tuple[SocketStream, int]:
        """Open a TCP connection with a timeout."""
        start = self._clock()

        try:
            with anyio.fail_after(config.connect_timeout):
                stream = await anyio.connect_tcp(host, config.port)
        except TimeoutError as exc:
            raise TcpCheckError(
                "connect_timeout",
                f"Connection to {host}:{config.port} timed out",
                retryable=True,
            ) from exc
        except OSError as exc:
            raise TcpCheckError(
                "connection_error",
                f"Connection to {host}:{config.port} failed: {exc}",
                retryable=True,
            ) from exc

        connect_time_ms = int((self._clock() - start) * 1000)
        return stream, connect_time_ms

    async def _negotiate_tls(
        self, stream: SocketStream, host: str, config: TcpCheckConfig
    ) -> tuple[TLSStream, int, Optional[dict[str, Any]]]:
        """Upgrade to TLS (implicit or STARTTLS) and return peer certificate."""
        if config.tls_mode == "starttls":
            await self._send_starttls_command(stream, config)

        tls_context = self._build_ssl_context(config)
        hostname = config.sni or host
        start = self._clock()

        try:
            with anyio.fail_after(config.tls_handshake_timeout):
                tls_stream = await TLSStream.wrap(
                    stream,
                    hostname=hostname,
                    ssl_context=tls_context,
                )
        except TimeoutError as exc:
            raise TcpCheckError(
                "tls_timeout",
                "TLS handshake timed out",
                retryable=True,
            ) from exc
        except Exception as exc:  # noqa: BLE001 - bubble up as structured error
            raise TcpCheckError(
                "tls_error",
                f"TLS handshake failed: {exc}",
                retryable=True,
            ) from exc

        tls_handshake_ms = int((self._clock() - start) * 1000)
        peer_cert: Any = tls_stream.extra(TLSAttribute.peer_certificate_binary)
        if not peer_cert:
            peer_cert = tls_stream.extra(TLSAttribute.peer_certificate)

        return tls_stream, tls_handshake_ms, peer_cert

    async def _send_starttls_command(
        self, stream: SocketStream, config: TcpCheckConfig
    ) -> None:
        """Send the STARTTLS command and ensure a positive response."""
        try:
            with anyio.fail_after(config.tls_handshake_timeout):
                await stream.send(config.starttls_command.encode())
                response = await stream.receive(1024)
        except TimeoutError as exc:
            raise TcpCheckError(
                "starttls_timeout",
                "Timed out waiting for STARTTLS response",
                retryable=True,
            ) from exc

        response_text = response.decode(errors="ignore").strip()
        if not self._is_positive_starttls_response(response_text):
            raise TcpCheckError(
                "starttls_rejected",
                f"STARTTLS rejected: {response_text or 'no response'}",
                retryable=False,
                data={"starttls_response": response_text},
            )

    def _build_ssl_context(self, config: TcpCheckConfig) -> ssl.SSLContext:
        """Create an SSL context based on verification requirements."""
        if config.verify:
            context = ssl.create_default_context()
        else:
            context = ssl._create_unverified_context()  # type: ignore[attr-defined]
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

        return context

    def _resolve_host(self, config: TcpCheckConfig, url: str) -> str | None:
        """Determine target host from config or check URL."""
        if config.host:
            return config.host

        if not url:
            return None

        parsed = urlparse(url)
        if parsed.hostname:
            return parsed.hostname

        return url

    def _cert_days_remaining(self, cert: Any) -> Optional[int]:
        """Calculate days remaining before certificate expiry."""
        if cert is None:
            return None

        cert_dict: dict[str, Any]

        if isinstance(cert, (bytes, bytearray)):
            pem_data = ssl.DER_cert_to_PEM_cert(cert)
            # stdlib lacks a public DER-to-dict helper; _test_decode_cert is stable enough for this
            with tempfile.NamedTemporaryFile(
                mode="w+", suffix=".pem", delete=False
            ) as tmp:
                tmp.write(pem_data)
                tmp.flush()
                temp_path = Path(tmp.name)

            try:
                cert_dict = ssl._ssl._test_decode_cert(str(temp_path))  # type: ignore[attr-defined]
            finally:
                temp_path.unlink(missing_ok=True)
        elif isinstance(cert, dict):
            cert_dict = cert
        else:
            return None

        not_after = cert_dict.get("notAfter")
        if not not_after:
            return None

        expires_at = ssl.cert_time_to_seconds(not_after)
        seconds_remaining = expires_at - time.time()
        return int(seconds_remaining // 86400)

    def _is_positive_starttls_response(self, response: str) -> bool:
        """Detect a successful STARTTLS response."""
        normalized = response.lower()
        numeric_code = normalized.split(" ", 1)[0]
        if numeric_code.isdigit():
            return numeric_code.startswith("2")

        return normalized.startswith("2") or "ok" in normalized

    def _error(
        self, check_id: int, error_type: str, message: str, extra: dict[str, Any]
    ) -> Result:
        """Build an error Result with standard fields."""
        data = {"error_type": error_type, "error_msg": message}
        data.update(extra)
        return Result(check_id=check_id, status=ResultStatus.ERROR, data=data)

    async def aclose(self) -> None:
        """Executor cleanup hook (no resources to release at the moment)."""
        return
