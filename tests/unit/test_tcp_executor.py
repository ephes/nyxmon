"""Unit tests for the TCP check executor."""

import asyncio
import socket
import ssl
from typing import Any, Awaitable, Callable

import anyio
import pytest

from nyxmon.adapters.runner.executors.tcp_executor import TcpCheckExecutor
from nyxmon.domain import Check, CheckType, ResultStatus


TEST_CERT = """-----BEGIN CERTIFICATE-----
MIIDCTCCAfGgAwIBAgIUBYmUQv1439DmIgLZdKWne+D5Z3swDQYJKoZIhvcNAQEL
BQAwFDESMBAGA1UEAwwJbG9jYWxob3N0MB4XDTI1MTIwOTE0MDcyOFoXDTI2MDEw
ODE0MDcyOFowFDESMBAGA1UEAwwJbG9jYWxob3N0MIIBIjANBgkqhkiG9w0BAQEF
AAOCAQ8AMIIBCgKCAQEAtBCKb4/dhti2+sksXZ+TlKWgQPTzIMaAy1n2SSCc233o
0GBFAaL/EGOewx0hr1wcPL0f8cbzk11gM7CxjpEH/VLtZD1VU6eehIQrOzECVhss
rgfkNThmwFs+Ao7Qg2/X8zC352La8YsrhnoSzzvm4w4s5pRr1esPvRjfbXvXKV6W
mz16N8O+4xXKGEPb7ZE4jMeZRKcSJH0crYtmP1cIJU2MTSUeh/cqDX3qfRneQEGq
yIgu4pva1yL97v+dtFpHiA0ODM0U8fjD+/JsMqFtB7M+NMXCBAo8OFn/PGmA6Ts0
UQJNuVOsLQARbbt9evMapcpt4+eK8Xq1LR7kpyzy9QIDAQABo1MwUTAdBgNVHQ4E
FgQU7A8+NuZotf1evj9kI/CV04/Tk8owHwYDVR0jBBgwFoAU7A8+NuZotf1evj9k
I/CV04/Tk8owDwYDVR0TAQH/BAUwAwEB/zANBgkqhkiG9w0BAQsFAAOCAQEAio3Z
gz2Gg2Cbzyo1Aa3s9hkbEeUyOteHQNhwOL3lBIM7JvmnJ1H4KkNMmqHheYbhhF7E
rqphF5HQHi4qnO5vWi6sby0WQruDDsc/B0aeL8DjGj9o6wB8yE6I+ZDLixDM/wqT
C5d3Sjxx7jJRJgNvti6MYWbYFZ6HP7BSFjDMznwOCPqd3d12nzTSnkZ7fblwHhYf
DvcA1OIaAukMb5oLJvIJE0PpL2c7SKjq6GQAF4xll0xmTcVXeqGnrXvieYK+cceX
GALJwdqTRFvQm0apM16gA0zBygIr1DWAV4e9dHnV4E+KBy3fb9GXzvk5ZKtYK6Xg
aJzg7otCsY0gBu1wfQ==
-----END CERTIFICATE-----"""

TEST_KEY = """-----BEGIN PRIVATE KEY-----
MIIEvwIBADANBgkqhkiG9w0BAQEFAASCBKkwggSlAgEAAoIBAQC0EIpvj92G2Lb6
ySxdn5OUpaBA9PMgxoDLWfZJIJzbfejQYEUBov8QY57DHSGvXBw8vR/xxvOTXWAz
sLGOkQf9Uu1kPVVTp56EhCs7MQJWGyyuB+Q1OGbAWz4CjtCDb9fzMLfnYtrxiyuG
ehLPO+bjDizmlGvV6w+9GN9te9cpXpabPXo3w77jFcoYQ9vtkTiMx5lEpxIkfRyt
i2Y/VwglTYxNJR6H9yoNfep9Gd5AQarIiC7im9rXIv3u/520WkeIDQ4MzRTx+MP7
8mwyoW0Hsz40xcIECjw4Wf88aYDpOzRRAk25U6wtABFtu3168xqlym3j54rxerUt
HuSnLPL1AgMBAAECggEAAL4VuA6NkQ4JOSEFvhAXpXQGZGYuL3sqEkyZa6VHCE+t
W1ieSDqyFxD2GWNgHW9BjY2RGWfi3r9yk1v963LVJ9oE8RYgqTLmgDDkVb7mvdCo
X0JYklCcedwWdh+9I+Gc8BuKEpnxga/7erc7px+d3N9U15GSnUP2IWc+Gp85XKoN
phyUD01ZLnboHTndkIqozzS6l4AmtmJQe4CqIVaS+ISpEnzwZUDd9Uhd4RuFtZ68
cqqJ51lqTg6NpE5xCuPQ2M6qlFqNB2emTScbfNWYyLD13dgF1wYgWP7lAFOHRqit
DWku4cbrkLFED1oZm4ZslwLJeR0UDMmKYQ+f1DxJQQKBgQD5Hw5FwdJdExmVAXqI
cHS7lwDJubPUrPgZwaQRuL9330nnAFGopIRwjwcvI6t7gMf2X3T9CWX14VnSO9Ru
tS3bVzOX+9seV6OMT+WAAKZRpZoem8Hh4tI4p+0HAttQIYphHvfrJueI7FYfvHPO
XZmG3MWW13+rcoa9GJe0Y9eNtQKBgQC5CVlu1MHMFYar2IXLaA8hcGl1kkxRkB+L
Bzw40MJfKi6pL3Mvm64zyXbQr/Z+FX3nLpf7uSy/zl8f/eyR6M9doNoKxM2awmw6
8BqGazX2Z1A/R6eUTcXAqnunvAOOJUd6TnjwOgo+neSsMXvShM8qsxyJd2YPdd5C
0YFfSSUYQQKBgQDW/XQlw0U2Sbt0GliS0uoK0iA99uM5ESTzpWdgW93xJ2Px1Raj
wYcCVIzQo6nj5ZmsB2lAzhGOBrKrejK0b+tpNXIzIYlSQDPGbVUUCHuATrgY3jaO
KF9fwZwOxupZ1vhDJKSz7Vk3ky4oKUyPtbs+5dwnd0aYwTeCjWyuotNtWQKBgQCr
x9w5IleQSeOuoeMERWTWnG+rcNhdWDmQbnUgId5xTs3mz2BWMGd3OG+PqexifT1X
ZFBAp1a98q8pGimIA+SPfYcvPCnMpPapeMKHS/za9mrvdGxFKDaQeTU3MTrzufQz
vapVCuz72MW0fnP/qsBRWdsCW9BqRfjDe5Bpj5RagQKBgQDGPB/Q7wMD6TvQDHvq
8xFBv070jHFVB4NWCRGL5/1LHLb3QTg1OXoFIlH7I5W3ZRTEiVqFcB0TOaO7QaeA
6w3Mnv6zlivAuWRQ3+zhJMNOtPNfNM1TQvNBMXCRbXZsxVgjdQZpZ34dsGH0xSmZ
O5WME8CEIwPzsZSfKFup6J1vIA==
-----END PRIVATE KEY-----"""


async def _plain_server(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        await reader.read(16)
    finally:
        writer.close()
        await writer.wait_closed()


def _tcp_check(port: int, **data: Any) -> Check:
    return Check(
        check_id=1,
        service_id=1,
        name="tcp",
        check_type=CheckType.TCP,
        url="127.0.0.1",
        data={"port": port, **data},
    )


@pytest.fixture()
def sni_check(tls_files):
    cert_path, key_path = tls_files
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)

    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.read(1)
        writer.close()
        await writer.wait_closed()

    async def _server():
        return await _start_server(handler, ssl_ctx=ssl_ctx)

    return _server


@pytest.fixture()
def tls_files(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_text(TEST_CERT)
    key_path.write_text(TEST_KEY)
    return cert_path, key_path


async def _start_server(
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
    *,
    ssl_ctx: ssl.SSLContext | None = None,
    port: int | None = None,
) -> tuple[asyncio.AbstractServer, int]:
    listen_port = 0 if port is None else port
    server = await asyncio.start_server(handler, "127.0.0.1", listen_port, ssl=ssl_ctx)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _start_starttls_server(
    cert: str, key: str
) -> tuple[asyncio.AbstractServer, int]:
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(certfile=cert, keyfile=key)

    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        request = await reader.readline()
        if request:
            writer.write(b"220 Ready to start TLS\r\n")
            await writer.drain()

            loop = asyncio.get_running_loop()
            transport = writer.transport
            tls_reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(tls_reader)
            tls_transport = await loop.start_tls(
                transport,
                protocol,
                ssl_ctx,
                server_side=True,
            )
            tls_writer = asyncio.StreamWriter(tls_transport, protocol, tls_reader, loop)
            await tls_reader.read(1)
            tls_writer.close()
            await tls_writer.wait_closed()
        else:
            writer.close()
            await writer.wait_closed()

    return await _start_server(handler)


@pytest.mark.anyio
async def test_plain_tcp_success() -> None:
    server, port = await _start_server(_plain_server)
    executor = TcpCheckExecutor()

    try:
        result = await executor.execute(_tcp_check(port))
    finally:
        server.close()
        await server.wait_closed()

    assert result.status == ResultStatus.OK
    assert result.data["connect_time_ms"] >= 0


@pytest.mark.anyio
async def test_connection_error_returns_error() -> None:
    # Bind to an ephemeral port and close to ensure nothing is listening
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    executor = TcpCheckExecutor()
    check = _tcp_check(port, connect_timeout=0.1, retries=0)

    result = await executor.execute(check)

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] in {"connection_error", "connect_timeout"}


@pytest.mark.anyio
async def test_implicit_tls_cert_expiry_warning(tls_files) -> None:
    cert_path, key_path = tls_files
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)

    server, port = await _start_server(_plain_server, ssl_ctx=ssl_ctx)
    executor = TcpCheckExecutor()

    try:
        result = await executor.execute(
            _tcp_check(
                port,
                tls_mode="implicit",
                check_cert_expiry=True,
                min_cert_days=999,  # force warning
                verify=False,
            )
        )
    finally:
        server.close()
        await server.wait_closed()

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "cert_expiry"
    assert "cert_days_remaining" in result.data
    assert result.data.get("severity") == "warning"


@pytest.mark.anyio
async def test_starttls_flow_succeeds(tls_files) -> None:
    cert_path, key_path = tls_files
    server, port = await _start_starttls_server(str(cert_path), str(key_path))
    executor = TcpCheckExecutor()

    try:
        result = await executor.execute(
            _tcp_check(
                port,
                tls_mode="starttls",
                verify=False,
                tls_handshake_timeout=2.0,
            )
        )
    finally:
        server.close()
        await server.wait_closed()

    assert result.status == ResultStatus.OK
    assert result.data["tls_handshake_ms"] >= 0


@pytest.mark.anyio
async def test_sni_override_used_in_tls_handshake(sni_check) -> None:
    """Executor should pass SNI override through TLS."""
    server, port = await sni_check()
    executor = TcpCheckExecutor()

    try:
        result = await executor.execute(
            _tcp_check(
                port,
                tls_mode="implicit",
                sni="override.example.com",
                verify=False,
            )
        )
    finally:
        server.close()
        await server.wait_closed()

    assert result.status == ResultStatus.OK


@pytest.mark.anyio
async def test_retries_transient_connection_failure() -> None:
    # Reserve a port that will become available shortly after the first attempt fails
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    async def delayed_server() -> None:
        await anyio.sleep(0.05)
        server, _ = await _start_server(_plain_server, port=port)
        await anyio.sleep(0.5)
        server.close()
        await server.wait_closed()

    executor = TcpCheckExecutor()
    check = _tcp_check(port, retries=1, retry_delay=0.1, connect_timeout=0.1)

    async with anyio.create_task_group() as tg:
        tg.start_soon(delayed_server)
        result = await executor.execute(check)
        tg.cancel_scope.cancel()

    assert result.status == ResultStatus.OK
    assert result.data["attempt"] == 2


# --- Protocol-aware STARTTLS -------------------------------------------------
#
# Real SMTP, IMAP and ManageSieve servers speak first and expect a protocol
# dialogue before STARTTLS. These fake servers model that so the executor has
# to read the greeting instead of mistaking it for the STARTTLS reply.

Responder = Callable[[bytes, dict[str, Any]], tuple[bytes, bool]]


async def _start_dialogue_server(
    cert: str, key: str, greeting: bytes, respond: Responder
) -> tuple[asyncio.AbstractServer, int, dict[str, Any]]:
    """Start a greet-first server that upgrades when ``respond`` says so."""
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(certfile=cert, keyfile=key)
    state: dict[str, Any] = {"lines": [], "upgraded": False}

    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            writer.write(greeting)
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    return
                state["lines"].append(line)
                reply, upgrade = respond(line, state)
                writer.write(reply)
                await writer.drain()
                if upgrade:
                    break
            loop = asyncio.get_running_loop()
            tls_reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(tls_reader)
            tls_transport = await loop.start_tls(
                writer.transport, protocol, ssl_ctx, server_side=True
            )
            state["upgraded"] = True
            tls_writer = asyncio.StreamWriter(tls_transport, protocol, tls_reader, loop)
            await tls_reader.read(1)
            tls_writer.close()
        except (ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    server, port = await _start_server(handler)
    return server, port, state


SMTP_GREETING = b"220-mail.test ESMTP\r\n220 mail.test ready\r\n"


def _smtp_responder(accept: bool = True) -> Responder:
    def respond(line: bytes, state: dict[str, Any]) -> tuple[bytes, bool]:
        command = line.strip().upper()
        if command.startswith(b"EHLO"):
            state["ehlo"] = True
            return b"250-mail.test\r\n250-PIPELINING\r\n250 STARTTLS\r\n", False
        if command == b"STARTTLS":
            if not state.get("ehlo"):
                return b"503 5.5.1 EHLO first\r\n", False
            if not accept:
                return b"454 4.7.0 TLS not available\r\n", False
            return b"220 2.0.0 Ready to start TLS\r\n", True
        return b"500 5.5.2 unknown command\r\n", False

    return respond


IMAP_GREETING = b"* OK [CAPABILITY IMAP4rev1 STARTTLS] mail.test ready\r\n"


def _imap_responder(accept: bool = True) -> Responder:
    def respond(line: bytes, state: dict[str, Any]) -> tuple[bytes, bool]:
        parts = line.strip().split(b" ", 1)
        if len(parts) == 2 and parts[1].upper() == b"STARTTLS":
            tag = parts[0]
            if not accept:
                return tag + b" NO STARTTLS unavailable\r\n", False
            return (
                b"* CAPABILITY IMAP4rev1\r\n" + tag + b" OK Begin TLS now\r\n",
                True,
            )
        return b"* BAD Error in IMAP command\r\n", False

    return respond


SIEVE_GREETING = (
    b'"IMPLEMENTATION" "Pigeonhole Sieve"\r\n'
    b'"SIEVE" "fileinto reject"\r\n'
    b'"STARTTLS"\r\n'
    b'OK "mail.test ready."\r\n'
)


def _sieve_responder(accept: bool = True) -> Responder:
    def respond(line: bytes, state: dict[str, Any]) -> tuple[bytes, bool]:
        if line.strip().upper() == b"STARTTLS":
            if not accept:
                return b'NO "TLS unavailable"\r\n', False
            return b'OK "Begin TLS negotiation now."\r\n', True
        return b'NO "Unknown command"\r\n', False

    return respond


PROTOCOL_SERVERS: dict[str, tuple[bytes, Callable[..., Responder]]] = {
    "smtp": (SMTP_GREETING, _smtp_responder),
    "imap": (IMAP_GREETING, _imap_responder),
    "sieve": (SIEVE_GREETING, _sieve_responder),
}


async def _run_protocol_check(
    tls_files: tuple[Any, Any],
    protocol: str,
    *,
    accept: bool = True,
    **data: Any,
) -> tuple[Any, dict[str, Any]]:
    cert_path, key_path = tls_files
    greeting, responder = PROTOCOL_SERVERS[protocol]
    server, port, state = await _start_dialogue_server(
        str(cert_path), str(key_path), greeting, responder(accept)
    )
    check_data: dict[str, Any] = {
        "tls_mode": "starttls",
        "verify": False,
        "tls_handshake_timeout": 2.0,
        "retries": 0,
    }
    check_data.update(data)
    try:
        result = await TcpCheckExecutor().execute(_tcp_check(port, **check_data))
    finally:
        server.close()
        await server.wait_closed()
    return result, state


@pytest.mark.anyio
@pytest.mark.parametrize("protocol", ["smtp", "imap", "sieve"])
async def test_starttls_protocol_dialogue_succeeds(tls_files, protocol) -> None:
    result, state = await _run_protocol_check(
        tls_files, protocol, starttls_protocol=protocol
    )

    assert result.status == ResultStatus.OK, result.data
    assert result.data["tls_handshake_ms"] >= 0
    assert result.data["starttls_protocol"] == protocol
    assert state["upgraded"] is True


@pytest.mark.anyio
async def test_starttls_smtp_sends_ehlo_before_starttls(tls_files) -> None:
    result, state = await _run_protocol_check(
        tls_files, "smtp", starttls_protocol="smtp"
    )

    assert result.status == ResultStatus.OK, result.data
    assert state["lines"] == [b"EHLO nyxmon.invalid\r\n", b"STARTTLS\r\n"]


@pytest.mark.anyio
async def test_starttls_imap_uses_tagged_command(tls_files) -> None:
    result, state = await _run_protocol_check(
        tls_files, "imap", starttls_protocol="imap"
    )

    assert result.status == ResultStatus.OK, result.data
    assert state["lines"] == [b"a1 STARTTLS\r\n"]


@pytest.mark.anyio
@pytest.mark.parametrize("protocol", ["smtp", "imap", "sieve"])
async def test_starttls_protocol_rejection_is_clean(tls_files, protocol) -> None:
    result, state = await _run_protocol_check(
        tls_files, protocol, accept=False, starttls_protocol=protocol
    )

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "starttls_rejected"
    assert result.data["starttls_stage"] == "starttls"
    assert result.data["starttls_response"]
    assert state["upgraded"] is False


@pytest.mark.anyio
@pytest.mark.parametrize("protocol", ["smtp", "imap", "sieve"])
async def test_starttls_protocol_reports_cert_expiry(tls_files, protocol) -> None:
    result, _ = await _run_protocol_check(
        tls_files,
        protocol,
        starttls_protocol=protocol,
        check_cert_expiry=True,
        min_cert_days=999,  # force the warning so the cert data is reported
    )

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "cert_expiry"
    assert isinstance(result.data["cert_days_remaining"], int)
    assert result.data["tls_handshake_ms"] >= 0


@pytest.mark.anyio
@pytest.mark.parametrize("protocol", ["smtp", "imap", "sieve"])
async def test_generic_starttls_against_greeting_server_fails(
    tls_files, protocol
) -> None:
    """Regression: the generic probe cannot talk to greet-first servers.

    Without a protocol it sends STARTTLS before reading the greeting. It used to
    take the greeting (``220 ...``, ``* OK ...``) as the STARTTLS reply and then
    fail the TLS handshake with ``tls_error`` against a healthy server.
    """
    result, state = await _run_protocol_check(tls_files, protocol)

    assert result.status == ResultStatus.ERROR
    assert state["upgraded"] is False


async def _run_custom_dialogue(
    tls_files: tuple[Any, Any], greeting: bytes, respond: Responder, **data: Any
) -> tuple[Any, dict[str, Any]]:
    cert_path, key_path = tls_files
    server, port, state = await _start_dialogue_server(
        str(cert_path), str(key_path), greeting, respond
    )
    check_data: dict[str, Any] = {
        "tls_mode": "starttls",
        "verify": False,
        "tls_handshake_timeout": 2.0,
        "retries": 0,
    }
    check_data.update(data)
    try:
        result = await TcpCheckExecutor().execute(_tcp_check(port, **check_data))
    finally:
        server.close()
        await server.wait_closed()
    return result, state


def _generic_responder(line: bytes, state: dict[str, Any]) -> tuple[bytes, bool]:
    if line.strip().upper() == b"STARTTLS":
        return b"220 go ahead\r\n", True
    return b"500 unknown\r\n", False


@pytest.mark.anyio
async def test_generic_starttls_can_read_greeting_first(tls_files) -> None:
    result, state = await _run_custom_dialogue(
        tls_files,
        b"220 custom service ready\r\n",
        _generic_responder,
        starttls_read_greeting=True,
    )

    assert result.status == ResultStatus.OK, result.data
    assert result.data["starttls_protocol"] == "generic"
    assert state["lines"] == [b"STARTTLS\r\n"]


@pytest.mark.anyio
async def test_imap_greeting_is_not_taken_as_starttls_reply(tls_files) -> None:
    """Regression: ``* OK`` must not count as the reply to ``a1 STARTTLS``."""

    def respond(line: bytes, state: dict[str, Any]) -> tuple[bytes, bool]:
        return b"a1 BAD ok, but no STARTTLS here\r\n", False

    result, state = await _run_custom_dialogue(
        tls_files, IMAP_GREETING, respond, starttls_protocol="imap"
    )

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "starttls_rejected"
    assert result.data["starttls_stage"] == "starttls"
    assert "a1 BAD" in result.data["starttls_response"]
    assert state["upgraded"] is False


@pytest.mark.anyio
async def test_imap_untagged_bye_is_rejected(tls_files) -> None:
    def respond(line: bytes, state: dict[str, Any]) -> tuple[bytes, bool]:
        return b"* BYE shutting down\r\n", False

    result, _ = await _run_custom_dialogue(
        tls_files, IMAP_GREETING, respond, starttls_protocol="imap"
    )

    assert result.data["error_type"] == "starttls_rejected"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("protocol", "greeting"),
    [
        ("smtp", b"554 5.3.2 service unavailable\r\n"),
        ("imap", b"* BYE too many connections\r\n"),
        ("sieve", b'BYE "too many connections"\r\n'),
    ],
)
async def test_negative_greeting_is_rejected(tls_files, protocol, greeting) -> None:
    _, responder = PROTOCOL_SERVERS[protocol]
    result, state = await _run_custom_dialogue(
        tls_files, greeting, responder(), starttls_protocol=protocol
    )

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "starttls_rejected"
    assert result.data["starttls_stage"] == "greeting"
    assert state["lines"] == []


@pytest.mark.anyio
async def test_smtp_ehlo_rejection_reports_stage(tls_files) -> None:
    def respond(line: bytes, state: dict[str, Any]) -> tuple[bytes, bool]:
        return b"550 5.7.1 go away\r\n", False

    result, _ = await _run_custom_dialogue(
        tls_files, SMTP_GREETING, respond, starttls_protocol="smtp"
    )

    assert result.data["error_type"] == "starttls_rejected"
    assert result.data["starttls_stage"] == "ehlo"


@pytest.mark.anyio
async def test_data_pipelined_after_starttls_reply_is_refused(tls_files) -> None:
    """Plain-text bytes sent before the handshake must not enter the TLS session."""

    def respond(line: bytes, state: dict[str, Any]) -> tuple[bytes, bool]:
        reply, upgrade = _smtp_responder()(line, state)
        if upgrade:
            reply += b"250 injected\r\n"
        return reply, upgrade

    result, _ = await _run_custom_dialogue(
        tls_files, SMTP_GREETING, respond, starttls_protocol="smtp"
    )

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "starttls_protocol_error"


@pytest.mark.anyio
async def test_overlong_greeting_line_is_refused(tls_files) -> None:
    result, _ = await _run_custom_dialogue(
        tls_files,
        b"220 " + b"x" * 8192 + b"\r\n",
        _smtp_responder(),
        starttls_protocol="smtp",
    )

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "starttls_protocol_error"


@pytest.mark.anyio
async def test_connection_closed_during_dialogue(tls_files) -> None:
    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writer.write(b"220-mail.test partial greeting\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _start_server(handler)
    try:
        result = await TcpCheckExecutor().execute(
            _tcp_check(
                port,
                tls_mode="starttls",
                starttls_protocol="smtp",
                retries=0,
                tls_handshake_timeout=2.0,
            )
        )
    finally:
        server.close()
        await server.wait_closed()

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "starttls_connection_closed"


def test_config_rejects_unknown_starttls_protocol() -> None:
    from nyxmon.domain.tcp_config import TcpCheckConfig

    config = TcpCheckConfig.from_dict(
        {"port": 25, "tls_mode": "starttls", "starttls_protocol": "pop3"}
    )
    with pytest.raises(ValueError, match="starttls_protocol"):
        config.validate()


def test_config_defaults_to_generic_and_round_trips() -> None:
    from nyxmon.domain.tcp_config import TcpCheckConfig

    legacy = TcpCheckConfig.from_dict({"port": 25, "tls_mode": "starttls"})
    assert legacy.starttls_protocol == "generic"
    assert legacy.starttls_read_greeting is False

    config = TcpCheckConfig.from_dict(
        {"port": 4190, "tls_mode": "starttls", "starttls_protocol": "sieve"}
    )
    assert config.validate()
    assert TcpCheckConfig.from_dict(config.to_dict()) == config
