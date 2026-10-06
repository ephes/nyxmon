"""Bounded HTTP reads shared by the HTTP and JSON-metrics executors."""

from __future__ import annotations

import zlib
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx

# Same default as httpx.
DEFAULT_MAX_REDIRECTS = 20


class BodyTooLargeError(Exception):
    """The response body exceeded the byte limit."""


class UnsupportedEncodingError(Exception):
    """The response uses a content encoding that cannot be decoded safely."""


def _port_or_default(url: httpx.URL) -> int | None:
    return url.port or {"http": 80, "https": 443}.get(url.scheme)


def _keeps_credentials(old: httpx.URL, new: httpx.URL) -> bool:
    """Whether httpx keeps ``Authorization`` on a redirect from ``old`` to ``new``."""
    if old.host != new.host:
        return False
    if old.scheme == new.scheme and _port_or_default(old) == _port_or_default(new):
        return True
    return (
        old.scheme == "http"
        and _port_or_default(old) == 80
        and new.scheme == "https"
        and _port_or_default(new) == 443
    )


def _inflater_for(encoding: str, head: bytes) -> Any:
    """Return a zlib decompressor for ``encoding`` given the body's first bytes.

    ``deflate`` is zlib-wrapped by the standard but sent raw by some servers;
    like httpx, accept both, choosing by whether ``head`` is a zlib header.
    """
    if encoding == "gzip":
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    is_zlib = (
        len(head) >= 2 and head[0] & 0x0F == 8 and ((head[0] << 8) | head[1]) % 31 == 0
    )
    return zlib.decompressobj(zlib.MAX_WBITS if is_zlib else -zlib.MAX_WBITS)


@asynccontextmanager
async def stream_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout: float,
    follow_redirects: bool,
    auth: Any = httpx.USE_CLIENT_DEFAULT,
    headers: dict[str, str] | None = None,
) -> AsyncIterator[httpx.Response]:
    """GET ``url`` as a stream and yield the final response, body unread.

    httpx reads the whole body of every redirect response it follows, before
    the caller sees the final response. Redirects are therefore followed here,
    one hop at a time, closing each redirect response without reading it.
    httpx still builds each next request, so headers such as
    ``Authorization`` are dropped on a cross-origin hop exactly as when httpx
    follows redirects itself. ``auth`` defaults to the client's own, as
    with ``client.get``. It is applied again on each hop while every hop so
    far stayed on the same origin (an http to https upgrade on the same host
    counts, as in httpx), so a challenge such as Digest at the redirect target
    still succeeds; after a cross-origin hop no credentials are sent.
    """
    max_redirects = getattr(client, "max_redirects", DEFAULT_MAX_REDIRECTS)
    async with client.stream(
        "GET",
        url,
        timeout=timeout,
        auth=auth,
        headers=headers,
        follow_redirects=False,
    ) as first:
        next_request: httpx.Request | None = getattr(first, "next_request", None)
        if not follow_redirects or next_request is None:
            yield first
            return

    hops = 0
    keep_auth = True
    previous_url = httpx.URL(url)
    while next_request is not None:
        hops += 1
        if hops > max_redirects:
            raise httpx.TooManyRedirects(
                "Exceeded maximum allowed redirects.", request=next_request
            )
        keep_auth = keep_auth and _keeps_credentials(previous_url, next_request.url)
        previous_url = next_request.url
        response = await client.send(
            next_request,
            auth=auth if keep_auth else None,
            follow_redirects=False,
            stream=True,
        )
        try:
            next_request = response.next_request
            if next_request is None:
                yield response
                return
        finally:
            await response.aclose()


async def read_capped(response: httpx.Response, limit: int) -> bytes:
    """Return the decoded body of ``response``, failing above ``limit`` bytes.

    The raw stream is decoded here rather than by httpx, which inflates each
    received chunk in full: a small compressed chunk could otherwise expand to
    many times the limit before it is counted. ``gzip`` and ``deflate`` are
    inflated with a bounded output size; any other encoding is refused.

    Raises:
        BodyTooLargeError: The declared or decoded body exceeds ``limit``.
        UnsupportedEncodingError: The ``Content-Encoding`` is not supported.
    """
    declared = response.headers.get("content-length")
    encodings = [
        token.strip().lower()
        for token in response.headers.get("content-encoding", "").split(",")
        if token.strip() and token.strip().lower() != "identity"
    ]
    if len(encodings) > 1 or (encodings and encodings[0] not in ("gzip", "deflate")):
        raise UnsupportedEncodingError(
            f"unsupported content encoding {response.headers['content-encoding']!r}"
        )
    if not encodings and declared is not None and declared.isdigit():
        if int(declared) > limit:
            raise BodyTooLargeError(
                f"response body of {declared} bytes exceeds the {limit} byte limit"
            )

    body = bytearray()

    def take(data: bytes) -> None:
        # One buffer, so empty outputs (a deflate stream of empty blocks)
        # cost nothing and memory tracks the decoded size alone.
        if len(body) + len(data) > limit:
            raise BodyTooLargeError(f"response body exceeds the {limit} byte limit")
        body.extend(data)

    inflater: Any = None

    def inflate(data: bytes) -> None:
        if inflater.eof:
            if data:
                raise ValueError("unexpected data after the compressed body")
            return
        while True:
            take(inflater.decompress(data, limit - len(body) + 1))
            if inflater.eof:
                # A second gzip member or trailing garbage would otherwise sit
                # in ``unused_data`` without counting towards the limit.
                if inflater.unused_data:
                    raise ValueError("unexpected data after the compressed body")
                return
            data = inflater.unconsumed_tail
            if not data:
                return

    pending = b""
    try:
        async for raw in response.aiter_raw():
            if not encodings:
                take(raw)
                continue
            if inflater is None:
                # The deflate framing is decided from the first two bytes.
                pending += raw
                if len(pending) < 2:
                    continue
                inflater = _inflater_for(encodings[0], pending)
                raw, pending = pending, b""
            inflate(raw)
        if encodings:
            if inflater is None and pending:
                inflater = _inflater_for(encodings[0], pending)
                inflate(pending)
            if inflater is not None:
                # Only output zlib still holds for input it already consumed;
                # ``take`` checks it against the limit like any other chunk.
                take(inflater.flush())
    except zlib.error as err:
        raise ValueError(f"invalid compressed body: {err}") from err
    return bytes(body)
