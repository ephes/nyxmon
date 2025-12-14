"""Unit tests for the JSON metrics executor."""

import anyio
import httpx

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

    async def get(self, url, auth=None, timeout=None):
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def aclose(self):
        return None


class StubResponse:
    def __init__(self, status_code: int, json_body):
        self.status_code = status_code
        self._json_body = json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise JsonMetricsError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self._json_body, Exception):
            raise self._json_body
        return self._json_body


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
