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
