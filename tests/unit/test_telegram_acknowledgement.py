"""Bounded acknowledgement handling for the active Django digest transport."""

import gzip
import time

import anyio
import httpx
import pytest

from nyxmon.adapters import notification

CLIENT = httpx.AsyncClient


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.closed = False
        self.read = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            await anyio.sleep(self.delay)
            self.read += len(chunk)
            yield chunk

    async def aclose(self):
        self.closed = True


def install_transport(monkeypatch, handler):
    clients = []

    def client():
        instance = CLIENT(transport=httpx.MockTransport(handler))
        clients.append(instance)
        return instance

    monkeypatch.setattr(notification.httpx, "AsyncClient", client)
    return clients


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        b'{"ok":false,"ok":true}',
        b'{"ok":true,"result":{"id":1,"id":2}}',
        b'{"ok":true,"result":NaN}',
        b'{"ok":true,"result":' + b"1" * 5000 + b"}",
        b'{"ok":true,"result":' + b"[" * 20000 + b"0" + b"]" * 20000 + b"}",
        b'{"ok":true,"result":"\xff"}',
    ],
    ids=[
        "duplicate-ok",
        "nested-duplicate",
        "nan",
        "huge-integer",
        "deep-array",
        "invalid-utf8",
    ],
)
async def test_invalid_json_acknowledgements_fail(monkeypatch, caplog, body):
    stream = ResponseStream([body])
    clients = install_transport(
        monkeypatch, lambda request: httpx.Response(200, stream=stream)
    )
    notifier = notification.AsyncTelegramNotifier(token="synthetic-token", chat_id="1")
    assert await notifier.async_send("warning") is False
    assert "delivered" not in caplog.text
    assert stream.closed and all(client.is_closed for client in clients)


@pytest.mark.anyio
async def test_oversized_reply_stops_reading_and_closes(monkeypatch, caplog):
    stream = ResponseStream([b"x" * 1024] * 1000)
    clients = install_transport(
        monkeypatch, lambda request: httpx.Response(200, stream=stream)
    )
    notifier = notification.AsyncTelegramNotifier(token="synthetic-token", chat_id="1")
    assert await notifier.async_send("warning") is False
    assert stream.read <= notification.TELEGRAM_MAX_RESPONSE_BYTES + 1024
    assert stream.closed and all(client.is_closed for client in clients)
    assert "xxxxx" not in caplog.text


@pytest.mark.anyio
async def test_compressed_reply_is_refused_before_expansion(monkeypatch):
    stream = ResponseStream([gzip.compress(b"x" * 1000000)])
    install_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, stream=stream, headers={"Content-Encoding": "gzip"}
        ),
    )
    notifier = notification.AsyncTelegramNotifier(token="synthetic-token", chat_id="1")
    assert await notifier.async_send("warning") is False
    assert stream.read == 0 and stream.closed


@pytest.mark.anyio
@pytest.mark.parametrize("fallback", [False, True])
async def test_whole_exchange_deadline_includes_fallback(monkeypatch, caplog, fallback):
    budget = 0.15
    monkeypatch.setattr(notification, "TELEGRAM_SEND_TIMEOUT_SECONDS", budget)
    streams = []

    async def respond(request):
        assert request.headers["accept-encoding"] == "identity"
        if fallback and not streams:
            # Spend part of the common budget before a valid parse refusal.
            await anyio.sleep(budget * 0.6)
            stream = ResponseStream(
                [b'{"ok":false,"description":"Bad Request: can\'t parse entities"}']
            )
            status = 400
        else:
            # A trickle stays active; an inactivity timeout alone cannot stop it.
            stream = ResponseStream([b" "] * 100, delay=budget * 0.1)
            status = 200
        streams.append(stream)
        return httpx.Response(status, stream=stream)

    clients = install_transport(monkeypatch, respond)
    notifier = notification.AsyncTelegramNotifier(token="synthetic-token", chat_id="1")
    start = time.monotonic()
    with anyio.fail_after(2):
        assert await notifier.async_send("warning") is False
    elapsed = time.monotonic() - start
    # Generous upper bound for loaded runners; still far below the 2 s guard.
    assert budget * 0.8 <= elapsed < budget + 1.0
    assert len(streams) == (2 if fallback else 1)
    assert all(stream.closed for stream in streams)
    assert all(client.is_closed for client in clients)
    assert "deadline exceeded" in caplog.text
    assert "synthetic-token" not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("status", [400, 413, 502])
@pytest.mark.parametrize(
    "body",
    [
        b"<html>synthetic-token proxy error</html>",
        b'{"proxy error":' + b"[" * 20000 + b"0" + b"]" * 20000 + b"}",
    ],
    ids=["html", "deep-json"],
)
async def test_non_json_error_status_is_logged_without_body(
    monkeypatch, caplog, status, body
):
    stream = ResponseStream([body])
    clients = install_transport(
        monkeypatch, lambda request: httpx.Response(status, stream=stream)
    )
    notifier = notification.AsyncTelegramNotifier(token="synthetic-token", chat_id="1")
    assert await notifier.async_send("warning") is False
    assert f"HTTP status {status}" in caplog.text
    assert "proxy error" not in caplog.text
    assert "synthetic-token" not in caplog.text
    assert stream.closed and all(client.is_closed for client in clients)
