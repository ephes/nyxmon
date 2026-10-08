"""Unit tests for the JSON metrics executor."""

import gzip
import json
from contextlib import asynccontextmanager

import anyio
import httpx
import pytest

from nyxmon.adapters.runner.executors.json_metrics_executor import (
    JsonMetricsExecutor,
    JsonMetricsError,
)
from nyxmon.domain import Check, ResultStatus


def _build_check(**data):
    return Check(
        check_id=data.get("check_id", 1),
        service_id=1,
        name=data.get("name", "JSON Metrics"),
        check_type="json-metrics",
        url=data.get("url", "http://localhost:9100/.well-known/health"),
        data=data.get(
            "config",
            {
                "url": "http://localhost:9100/.well-known/health",
                "checks": [
                    {
                        "path": "$.mail.queue_total",
                        "op": "<",
                        "value": 100,
                        "severity": "warning",
                    }
                ],
            },
        ),
    )


class StubClient:
    """Async stub for httpx.AsyncClient."""

    def __init__(self, response):
        self.response = response
        self.calls = 0

    @asynccontextmanager
    async def stream(
        self, method, url, auth=None, timeout=None, headers=None, follow_redirects=None
    ):
        assert method == "GET"
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        yield self.response

    async def aclose(self):
        return None


class StubResponse:
    def __init__(self, status_code: int, json_body, headers=None):
        self.status_code = status_code
        self._json_body = json_body
        self.headers = headers or {}
        self.next_request = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise JsonMetricsError(f"HTTP {self.status_code}")

    async def aiter_bytes(self):
        if isinstance(self._json_body, bytes):
            yield self._json_body
        elif isinstance(self._json_body, AssertionError):
            raise self._json_body
        elif isinstance(self._json_body, Exception):
            yield b"{not json"
        else:
            yield json.dumps(self._json_body).encode()


def test_successful_thresholds_pass() -> None:
    client = StubClient(
        StubResponse(
            200,
            {"mail": {"queue_total": 5}, "services": {"postfix": "active"}},
        )
    )
    executor = JsonMetricsExecutor(client=client)

    check = _build_check(
        config={
            "url": "http://h",
            "checks": [
                {
                    "path": "$.mail.queue_total",
                    "op": "<",
                    "value": 100,
                    "severity": "warning",
                },
                {
                    "path": "$.services.postfix",
                    "op": "==",
                    "value": "active",
                    "severity": "critical",
                },
            ],
        }
    )

    result = anyio.run(executor.execute, check)

    assert result.status == ResultStatus.OK
    assert "failures" not in result.data


def test_threshold_failure_returns_warning_for_warning_severity() -> None:
    """Warning-severity threshold failures return WARNING status."""
    client = StubClient(
        StubResponse(
            200,
            {"mail": {"queue_total": 150}},
        )
    )
    executor = JsonMetricsExecutor(client=client)

    check = _build_check()

    result = anyio.run(executor.execute, check)

    # Warning-only threshold breaches return WARNING, not ERROR
    assert result.status == ResultStatus.WARNING
    assert result.data["error_type"] == "threshold_failed"
    assert result.data["failures"][0]["severity"] == "warning"
    assert "error_msg" in result.data  # Failure summary included


def test_threshold_failure_returns_error_for_critical_severity() -> None:
    """Critical-severity threshold failures return ERROR status."""
    client = StubClient(
        StubResponse(
            200,
            {"mail": {"queue_total": 150}},
        )
    )
    executor = JsonMetricsExecutor(client=client)

    check = _build_check(
        config={
            "url": "http://localhost:9100/.well-known/health",
            "checks": [
                {
                    "path": "$.mail.queue_total",
                    "op": "<",
                    "value": 100,
                    "severity": "critical",
                }
            ],
        }
    )

    result = anyio.run(executor.execute, check)

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "threshold_failed"
    assert result.data["failures"][0]["severity"] == "critical"
    assert "error_msg" in result.data  # Failure summary included


def test_handles_http_error() -> None:
    client = StubClient(StubResponse(500, {"error": "boom"}))
    executor = JsonMetricsExecutor(client=client)
    check = _build_check()

    result = anyio.run(executor.execute, check)

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "http_error"


def test_invalid_config_short_circuits() -> None:
    executor = JsonMetricsExecutor(client=StubClient(StubResponse(200, {})))
    check = _build_check(config={"url": "", "checks": []})

    result = anyio.run(executor.execute, check)

    assert result.status == ResultStatus.ERROR
    assert "configuration" in result.data["error_type"]


def test_json_parse_error() -> None:
    client = StubClient(StubResponse(200, ValueError("bad json")))
    executor = JsonMetricsExecutor(client=client)
    check = _build_check()

    result = anyio.run(executor.execute, check)

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "json_error"


def test_path_missing_counts_as_failure() -> None:
    """Missing path in response counts as threshold failure."""
    client = StubClient(StubResponse(200, {"services": {}}))
    executor = JsonMetricsExecutor(client=client)
    check = _build_check()

    result = anyio.run(executor.execute, check)

    # Default check uses warning severity, so returns WARNING
    assert result.status == ResultStatus.WARNING
    assert result.data["failures"][0]["actual"] is None


def test_bracket_array_path_is_supported() -> None:
    client = StubClient(StubResponse(200, {"disks": [{"ok": True}]}))
    executor = JsonMetricsExecutor(client=client)
    check = _build_check(
        config={
            "url": "http://h",
            "checks": [
                {
                    "path": "$.disks[0].ok",
                    "op": "==",
                    "value": True,
                    "severity": "critical",
                }
            ],
        }
    )

    result = anyio.run(executor.execute, check)

    assert result.status == ResultStatus.OK


def test_timeout_retries_then_fails(monkeypatch) -> None:
    timeout_exc = httpx.TimeoutException("boom")
    client = StubClient(timeout_exc)
    executor = JsonMetricsExecutor(client=client)
    check = _build_check(
        config={
            "url": "http://h",
            "checks": [{"path": "$.a", "op": "<", "value": 1, "severity": "warning"}],
            "retries": 1,
            "retry_delay": 0,
        }
    )

    async def _fast_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "nyxmon.adapters.runner.executors.json_metrics_executor.anyio.sleep",
        _fast_sleep,
    )

    result = anyio.run(executor.execute, check)

    assert client.calls == 2
    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "timeout"


def _rule_check(path, op, value, severity="critical", **extra):
    return _build_check(
        config={
            "url": "http://h",
            "checks": [{"path": path, "op": op, "value": value, "severity": severity}],
            **extra,
        }
    )


@pytest.mark.parametrize(
    ("path", "op", "value"),
    [
        ("$.status", "!=", "error"),
        ("$.status", "==", None),
        ("$.nested.status", "!=", "error"),
        ("$.items[5]", "!=", 0),
    ],
)
def test_missing_path_fails_for_every_operator(path, op, value) -> None:
    """A field the endpoint stopped sending must not satisfy the rule."""
    client = StubClient(StubResponse(200, {"items": [1], "other": "x"}))
    executor = JsonMetricsExecutor(client=client)

    result = anyio.run(executor.execute, _rule_check(path, op, value))

    assert result.status == ResultStatus.ERROR
    failure = result.data["failures"][0]
    assert failure["reason"] == "path_missing"
    assert failure["actual"] is None


@pytest.mark.parametrize(
    ("payload", "path", "op", "value"),
    [
        ({"status": "ok"}, "$.status", "!=", "error"),
        ({"status": None}, "$.status", "==", None),
        ({"items": [0, 7]}, "$.items[1]", "==", 7),
    ],
)
def test_present_path_still_passes(payload, path, op, value) -> None:
    client = StubClient(StubResponse(200, payload))
    executor = JsonMetricsExecutor(client=client)

    result = anyio.run(executor.execute, _rule_check(path, op, value))

    assert result.status == ResultStatus.OK


def test_present_null_that_fails_has_no_missing_reason() -> None:
    client = StubClient(StubResponse(200, {"status": None}))
    executor = JsonMetricsExecutor(client=client)

    result = anyio.run(executor.execute, _rule_check("$.status", "==", "ok"))

    failure = result.data["failures"][0]
    assert failure["actual"] is None
    assert "reason" not in failure


def test_large_actual_is_truncated() -> None:
    payload = {"blob": "x" * 5000, "list": list(range(1000))}
    client = StubClient(StubResponse(200, payload))
    executor = JsonMetricsExecutor(client=client)

    result = anyio.run(executor.execute, _rule_check("$", "==", "something else"))

    failure = result.data["failures"][0]
    assert failure["actual_truncated"] is True
    assert isinstance(failure["actual"], str)
    assert len(failure["actual"]) <= 201


def test_small_actual_is_kept_as_is() -> None:
    client = StubClient(StubResponse(200, {"n": [1, 2, 3]}))
    executor = JsonMetricsExecutor(client=client)

    result = anyio.run(executor.execute, _rule_check("$.n", "==", []))

    failure = result.data["failures"][0]
    assert failure["actual"] == [1, 2, 3]
    assert "actual_truncated" not in failure


def test_declared_content_length_over_cap_fails_without_reading() -> None:
    response = StubResponse(
        200, AssertionError("must not be read"), headers={"content-length": "2000"}
    )
    executor = JsonMetricsExecutor(client=StubClient(response))

    result = anyio.run(
        executor.execute, _rule_check("$.ok", "==", True, max_body_bytes=1000)
    )

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "body_too_large"


def test_streamed_body_over_cap_fails() -> None:
    """A body without Content-Length is cut off once it passes the cap."""
    read: list[int] = []

    async def chunks():
        for _ in range(100):
            read.append(1)
            yield b" " * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            executor = JsonMetricsExecutor(client=c)
            return await executor.execute(
                _rule_check("$.ok", "==", True, max_body_bytes=4096)
            )

    result = anyio.run(run)

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "body_too_large"
    assert len(read) <= 5


def _streamed(body: bytes, chunk_size: int = 1024):
    """Response content that is streamed like a network body, not pre-read."""

    async def chunks():
        for start in range(0, len(body), chunk_size):
            yield body[start : start + chunk_size]

    return chunks()


def test_body_within_cap_is_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_streamed(b'{"ok": true}'))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            executor = JsonMetricsExecutor(client=c)
            return await executor.execute(
                _rule_check("$.ok", "==", True, max_body_bytes=64)
            )

    assert anyio.run(run).status == ResultStatus.OK


def test_error_response_body_is_not_read() -> None:
    response = StubResponse(404, AssertionError("must not be read"))
    executor = JsonMetricsExecutor(client=StubClient(response))

    result = anyio.run(executor.execute, _rule_check("$.ok", "==", True))

    assert result.data["error_type"] == "http_error"


def test_cap_applies_to_decompressed_bytes() -> None:
    """A small gzip body that inflates past the cap is refused."""
    body = gzip.compress(json.dumps({"ok": True, "pad": "x" * 10000}).encode())
    assert len(body) < 1000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, content=_streamed(body)
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            executor = JsonMetricsExecutor(client=c)
            return await executor.execute(
                _rule_check("$.ok", "==", True, max_body_bytes=1000)
            )

    result = anyio.run(run)

    assert result.data["error_type"] == "body_too_large"
