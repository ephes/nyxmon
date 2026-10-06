"""Tests for the bounded HTTP reads used by the HTTP and JSON-metrics checks."""

from __future__ import annotations

import gzip
import json
import tracemalloc
import zlib
from typing import Any, AsyncIterator, Callable

import anyio
import httpx
import pytest

from nyxmon.adapters.runner.executors.http_executor import HttpCheckExecutor
from nyxmon.adapters.runner.executors.json_metrics_executor import (
    JsonMetricsExecutor,
)
from nyxmon.domain import Check, CheckType, ResultStatus


class UnreadableBody(httpx.AsyncByteStream):
    """A body that fails the test if anything reads it."""

    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        raise AssertionError("this response body must not be read")
        yield b""  # pragma: no cover - makes this an async generator

    async def aclose(self) -> None:
        self.closed = True


def streamed(body: bytes, chunk_size: int = 4096) -> AsyncIterator[bytes]:
    async def chunks() -> AsyncIterator[bytes]:
        for start in range(0, len(body), chunk_size):
            yield body[start : start + chunk_size]

    return chunks()


def metrics_check(url: str = "https://a.example/health", **extra: Any) -> Check:
    return Check(
        check_id=1,
        service_id=1,
        check_type=CheckType.JSON_METRICS,
        url=url,
        data={
            "url": url,
            "retries": 0,
            "checks": [
                {"path": "$.ok", "op": "==", "value": True, "severity": "critical"}
            ],
            **extra,
        },
    )


def run_metrics(
    handler: Callable[[httpx.Request], httpx.Response], check: Check, **client: Any
):
    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            transport=transport, follow_redirects=True, **client
        ) as c:
            return await JsonMetricsExecutor(client=c).execute(check)

    return anyio.run(run)


def test_redirect_body_is_not_read_by_json_metrics() -> None:
    redirect_body = UnreadableBody()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                302, headers={"location": "/v2/health"}, stream=redirect_body
            )
        return httpx.Response(200, content=streamed(b'{"ok": true}'))

    result = run_metrics(handler, metrics_check(max_body_bytes=1024))

    assert result.status == ResultStatus.OK, result.data
    assert redirect_body.closed


def test_redirect_body_is_not_read_by_http_check() -> None:
    redirect_body = UnreadableBody()
    final_body = UnreadableBody()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/":
            return httpx.Response(
                301, headers={"location": "/new"}, stream=redirect_body
            )
        return httpx.Response(200, stream=final_body)

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as c:
            check = Check(
                check_id=1,
                service_id=1,
                check_type=CheckType.HTTP,
                url="https://a.example/",
                data={},
            )
            return await HttpCheckExecutor(client=c).execute(check)

    result = anyio.run(run)

    assert result.status == ResultStatus.OK, result.data
    assert seen == ["/", "/new"]
    assert redirect_body.closed and final_body.closed


def test_http_check_without_follow_returns_the_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/"
        return httpx.Response(301, headers={"location": "https://b.example/"})

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as c:
            check = Check(
                check_id=1,
                service_id=1,
                check_type=CheckType.HTTP,
                url="https://a.example/",
                data={
                    "follow_redirects": False,
                    "expected_status": 301,
                    "expected_location": "https://b.example/",
                },
            )
            return await HttpCheckExecutor(client=c).execute(check)

    assert anyio.run(run).status == ResultStatus.OK


@pytest.mark.parametrize(
    ("location", "expect_auth"),
    [("/same-origin", True), ("https://other.example/health", False)],
)
def test_redirect_keeps_auth_only_on_the_same_origin(
    location: str, expect_auth: bool
) -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host + request.url.path] = request.headers.get("authorization")
        if request.url.path == "/health" and request.url.host == "a.example":
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, content=streamed(b'{"ok": true}'))

    result = run_metrics(
        handler, metrics_check(auth={"username": "nyx", "password": "mon"})
    )

    assert result.status == ResultStatus.OK, result.data
    assert seen["a.example/health"] is not None
    final = [value for key, value in seen.items() if key != "a.example/health"]
    assert (final[0] is not None) is expect_auth


def test_redirect_loop_is_bounded() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": f"/hop{calls}"})

    result = run_metrics(handler, metrics_check(), max_redirects=3)

    assert result.data["error_type"] == "request_error"
    assert calls == 4


def test_requests_only_encodings_it_can_bound() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("accept-encoding"))
        return httpx.Response(200, content=streamed(b'{"ok": true}'))

    run_metrics(handler, metrics_check())

    assert seen == ["gzip, deflate"]


@pytest.mark.parametrize(
    ("encoding", "compress"),
    [
        ("gzip", gzip.compress),
        ("deflate", zlib.compress),
    ],
)
def test_compressed_body_within_cap_is_parsed(
    encoding: str, compress: Callable[[bytes], bytes]
) -> None:
    body = compress(json.dumps({"ok": True, "pad": "x" * 500}).encode())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-encoding": encoding}, content=streamed(body)
        )

    result = run_metrics(handler, metrics_check(max_body_bytes=1024))

    assert result.status == ResultStatus.OK, result.data


def test_compression_bomb_is_stopped_at_the_cap() -> None:
    """32 MiB of zeros gzip to ~32 KiB; inflating must stop near the cap."""
    bomb = gzip.compress(b"0" * (32 * 1024 * 1024))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=streamed(bomb, chunk_size=len(bomb)),
        )

    tracemalloc.start()
    try:
        result = run_metrics(handler, metrics_check(max_body_bytes=1024 * 1024))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.data["error_type"] == "body_too_large"
    assert peak < 8 * 1024 * 1024


def test_unsupported_encoding_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-encoding": "br"}, content=streamed(b"\x00\x01")
        )

    result = run_metrics(handler, metrics_check())

    assert result.data["error_type"] == "unsupported_encoding"


def test_corrupt_compressed_body_is_a_json_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=streamed(b"definitely not gzip"),
        )

    result = run_metrics(handler, metrics_check())

    assert result.data["error_type"] == "json_error"


def _raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


@pytest.mark.parametrize("chunk_size", [1, 4096])
@pytest.mark.parametrize("framing", ["zlib", "raw"])
def test_deflate_accepts_zlib_and_raw_framing(framing: str, chunk_size: int) -> None:
    payload = json.dumps({"ok": True}).encode()
    body = zlib.compress(payload) if framing == "zlib" else _raw_deflate(payload)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "deflate"},
            content=streamed(body, chunk_size=chunk_size),
        )

    result = run_metrics(handler, metrics_check())

    assert result.status == ResultStatus.OK, result.data


@pytest.mark.parametrize(
    "trailer",
    [
        gzip.compress(b" " * 4096),  # a second gzip member
        b"\x00" * (8 * 1024 * 1024),  # trailing garbage
    ],
)
def test_data_after_the_gzip_member_is_refused(trailer: bytes) -> None:
    body = gzip.compress(b'{"ok": true}') + trailer

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, content=streamed(body)
        )

    tracemalloc.start()
    try:
        result = run_metrics(handler, metrics_check(max_body_bytes=1024))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.data["error_type"] == "json_error"
    assert "after the compressed body" in result.data["error_msg"]
    assert peak < 4 * 1024 * 1024


def test_http_check_keeps_the_clients_own_auth() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200)

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, auth=("u", "p")) as c:
            check = Check(
                check_id=1,
                service_id=1,
                check_type=CheckType.HTTP,
                url="https://a.example/",
                data={},
            )
            return await HttpCheckExecutor(client=c).execute(check)

    assert anyio.run(run).status == ResultStatus.OK
    assert seen == ["Basic dTpw"]  # base64("u:p")


def test_empty_deflate_blocks_do_not_accumulate() -> None:
    """Empty blocks inflate to nothing and must not cost memory per block."""
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    parts = [compressor.flush(zlib.Z_SYNC_FLUSH) for _ in range(2_000_000)]
    parts.append(compressor.compress(b'{"ok": true}') + compressor.flush())
    body = b"".join(parts)
    del parts

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "deflate"},
            content=streamed(body, chunk_size=64 * 1024),
        )

    tracemalloc.start()
    try:
        result = run_metrics(handler, metrics_check(max_body_bytes=1024))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.status == ResultStatus.OK, result.data
    assert peak < 4 * 1024 * 1024


def _digest_handler(seen: list[tuple[str, str | None]]):
    challenge = 'Digest realm="nyx", nonce="abc", qop="auth", algorithm=MD5'

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization")
        seen.append((request.url.host + request.url.path, auth))
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": request.headers["x-to"]})
        if auth is None or not auth.startswith("Digest "):
            return httpx.Response(401, headers={"www-authenticate": challenge})
        return httpx.Response(200)

    return handler


@pytest.mark.parametrize(
    ("target", "expected"),
    [("/dest", ResultStatus.OK), ("https://other.example/dest", ResultStatus.ERROR)],
)
def test_challenge_auth_runs_again_only_after_same_origin_redirects(
    target: str, expected: str
) -> None:
    seen: list[tuple[str, str | None]] = []

    async def run():
        transport = httpx.MockTransport(_digest_handler(seen))
        async with httpx.AsyncClient(
            transport=transport,
            auth=httpx.DigestAuth("u", "p"),
            headers={"x-to": target},
        ) as c:
            check = Check(
                check_id=1,
                service_id=1,
                check_type=CheckType.HTTP,
                url="https://a.example/",
                data={},
            )
            return await HttpCheckExecutor(client=c).execute(check)

    result = anyio.run(run)

    assert result.status == expected, result.data
    if expected == ResultStatus.ERROR:
        # No credentials, not even a Digest answer, cross the origin.
        assert all(auth is None for host, auth in seen if host.startswith("other"))
