"""Unit tests for the plain HTTP check executor."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator

import httpx
import pytest

from nyxmon.adapters.runner.executors.http_executor import HttpCheckExecutor
from nyxmon.domain import Check, CheckType, ResultStatus


class StubClient:
    """Async stub for httpx.AsyncClient."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls = 0
        self.timeouts: list[float | None] = []
        self.follow_redirects: list[bool] = []

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        timeout: float | None = None,
        follow_redirects: bool = True,
        auth: Any = None,
        headers: Any = None,
    ) -> AsyncIterator[Any]:
        assert method == "GET"
        del url, auth, headers
        self.calls += 1
        self.timeouts.append(timeout)
        self.follow_redirects.append(follow_redirects)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        yield response

    async def aclose(self) -> None:
        return None


class StubResponse:
    def __init__(self, status_code: int, location: str | None = None) -> None:
        self.status_code = status_code
        self.headers = {} if location is None else {"location": location}


def _build_check(**data: Any) -> Check:
    return Check(
        check_id=data.get("check_id", 1),
        service_id=1,
        name=data.get("name", "HTTP"),
        check_type=CheckType.HTTP,
        url=data.get("url", "https://example.test/health"),
        data=data.get("config", {}),
    )


async def _fast_sleep(*_args: Any, **_kwargs: Any) -> None:
    return None


@pytest.mark.anyio
async def test_success_with_no_config_remains_ok() -> None:
    client = StubClient([StubResponse(200)])
    executor = HttpCheckExecutor(client=client)  # type: ignore[arg-type]

    result = await executor.execute(_build_check())

    assert result.status == ResultStatus.OK
    assert result.data == {}
    assert client.calls == 1
    assert client.timeouts == [10.0]
    assert client.follow_redirects == [True]


@pytest.mark.anyio
async def test_exact_redirect_contract_succeeds_without_following() -> None:
    location = "https://fabian-heis.de/probe/path?query=preserved"
    client = StubClient([StubResponse(301, location=location)])
    executor = HttpCheckExecutor(client=client)  # type: ignore[arg-type]

    result = await executor.execute(
        _build_check(
            config={
                "follow_redirects": False,
                "expected_status": 301,
                "expected_location": location,
            }
        )
    )

    assert result.status == ResultStatus.OK
    assert result.data == {"status_code": 301, "location": location}
    assert client.follow_redirects == [False]


@pytest.mark.anyio
async def test_exact_redirect_contract_retries_transient_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "nyxmon.adapters.runner.executors.http_executor.anyio.sleep",
        _fast_sleep,
    )
    location = "https://fabian-heis.de/probe/path?query=preserved"
    client = StubClient([StubResponse(502), StubResponse(301, location=location)])
    executor = HttpCheckExecutor(client=client)  # type: ignore[arg-type]

    result = await executor.execute(
        _build_check(
            config={
                "follow_redirects": False,
                "expected_status": 301,
                "expected_location": location,
                "retries": 1,
                "retry_delay": 0,
            }
        )
    )

    assert result.status == ResultStatus.OK
    assert result.data["attempt"] == 2
    assert client.calls == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (
            StubResponse(302, location="https://fabian-heis.de/probe"),
            "unexpected_status",
        ),
        (
            StubResponse(301, location="https://wrong.example/probe"),
            "unexpected_location",
        ),
    ],
)
async def test_redirect_contract_rejects_status_or_location_drift(
    response: StubResponse, error_type: str
) -> None:
    client = StubClient([response])
    executor = HttpCheckExecutor(client=client)  # type: ignore[arg-type]

    result = await executor.execute(
        _build_check(
            config={
                "follow_redirects": False,
                "expected_status": 301,
                "expected_location": "https://fabian-heis.de/probe",
            }
        )
    )

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == error_type


@pytest.mark.anyio
async def test_502_then_200_succeeds_when_retries_allow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "nyxmon.adapters.runner.executors.http_executor.anyio.sleep",
        _fast_sleep,
    )
    client = StubClient([StubResponse(502), StubResponse(200)])
    executor = HttpCheckExecutor(client=client)  # type: ignore[arg-type]

    result = await executor.execute(
        _build_check(config={"retries": 3, "retry_delay": 0})
    )

    assert result.status == ResultStatus.OK
    assert result.data["attempt"] == 2
    assert result.data["attempts"] == 4
    assert client.calls == 2


@pytest.mark.anyio
async def test_exhausted_502_retries_returns_error_with_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "nyxmon.adapters.runner.executors.http_executor.anyio.sleep",
        _fast_sleep,
    )
    client = StubClient([StubResponse(502), StubResponse(502)])
    executor = HttpCheckExecutor(client=client)  # type: ignore[arg-type]

    result = await executor.execute(
        _build_check(config={"retries": 1, "retry_delay": 0})
    )

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "http_error"
    assert result.data["status_code"] == 502
    assert result.data["attempt"] == 2
    assert result.data["attempts"] == 2
    assert client.calls == 2


@pytest.mark.anyio
async def test_404_does_not_retry_by_default() -> None:
    client = StubClient([StubResponse(404), StubResponse(200)])
    executor = HttpCheckExecutor(client=client)  # type: ignore[arg-type]

    result = await executor.execute(
        _build_check(config={"retries": 3, "retry_delay": 0})
    )

    assert result.status == ResultStatus.ERROR
    assert result.data["status_code"] == 404
    assert result.data["attempts"] == 4
    assert client.calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("status_code", [100, 304])
async def test_non_2xx_status_is_error(status_code: int) -> None:
    client = StubClient([StubResponse(status_code)])
    executor = HttpCheckExecutor(client=client)  # type: ignore[arg-type]

    result = await executor.execute(_build_check())

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "http_error"
    assert result.data["status_code"] == status_code
    assert client.calls == 1


@pytest.mark.anyio
async def test_timeout_retries_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nyxmon.adapters.runner.executors.http_executor.anyio.sleep",
        _fast_sleep,
    )
    client = StubClient(
        [
            httpx.TimeoutException("boom"),
            StubResponse(200),
        ]
    )
    executor = HttpCheckExecutor(client=client)  # type: ignore[arg-type]

    result = await executor.execute(
        _build_check(config={"timeout": 3.0, "retries": 1, "retry_delay": 0})
    )

    assert result.status == ResultStatus.OK
    assert result.data["attempt"] == 2
    assert client.calls == 2
    assert client.timeouts == [3.0, 3.0]


@pytest.mark.anyio
async def test_request_error_retries_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "nyxmon.adapters.runner.executors.http_executor.anyio.sleep",
        _fast_sleep,
    )
    request = httpx.Request("GET", "https://example.test/health")
    client = StubClient(
        [httpx.RequestError("broken", request=request), StubResponse(200)]
    )
    executor = HttpCheckExecutor(client=client)  # type: ignore[arg-type]

    result = await executor.execute(
        _build_check(config={"retries": 1, "retry_delay": 0})
    )

    assert result.status == ResultStatus.OK
    assert result.data["attempt"] == 2
    assert client.calls == 2


@pytest.mark.anyio
async def test_invalid_config_returns_configuration_error() -> None:
    client = StubClient([StubResponse(200)])
    executor = HttpCheckExecutor(client=client)  # type: ignore[arg-type]

    result = await executor.execute(_build_check(config={"timeout": 0}))

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
    assert client.calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "config",
    [
        None,
        ["not", "an", "object"],
        {"timeout": {}},
        {"retry_status_codes": None},
        {"retry_status_codes": "502"},
    ],
)
async def test_malformed_config_returns_configuration_error(config: Any) -> None:
    client = StubClient([StubResponse(200)])
    executor = HttpCheckExecutor(client=client)  # type: ignore[arg-type]

    result = await executor.execute(_build_check(config=config))

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
    assert client.calls == 0


class _UnreadableBody(httpx.AsyncByteStream):
    """A response body that fails the test if anything reads it."""

    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        raise AssertionError("the HTTP check must not read the response body")
        yield b""  # pragma: no cover - makes this an async generator

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.anyio
async def test_body_is_never_read_and_the_stream_is_closed() -> None:
    body = _UnreadableBody()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "999999999"}, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpCheckExecutor(client=client)
        result = await executor.execute(_build_check(config={"expected_status": 200}))

    assert result.status == ResultStatus.OK
    assert result.data == {"status_code": 200}
    assert body.closed


@pytest.mark.anyio
async def test_attempt_duration_survives_wall_clock_rollback(monkeypatch) -> None:
    wall_seconds = 1000
    monotonic_ns = 0
    retry_delays = []
    monkeypatch.setattr(
        "nyxmon.adapters.runner.executors.http_executor.time",
        SimpleNamespace(time=lambda: wall_seconds, monotonic_ns=lambda: monotonic_ns),
    )

    async def retry_sleep(delay: float) -> None:
        nonlocal monotonic_ns
        retry_delays.append(delay)
        monotonic_ns += 1_000_000_000

    monkeypatch.setattr(
        "nyxmon.adapters.runner.executors.http_executor.anyio.sleep",
        retry_sleep,
    )

    class ClockRollbackBody(_UnreadableBody):
        async def aclose(self) -> None:
            nonlocal wall_seconds, monotonic_ns
            wall_seconds -= 30
            monotonic_ns += 125_000_000
            await super().aclose()

    responses = iter(
        [httpx.Response(502), httpx.Response(200, stream=ClockRollbackBody())]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpCheckExecutor(client=client)
        result = await executor.execute(
            _build_check(config={"retries": 1, "retry_delay": 1})
        )

    assert result.status == ResultStatus.OK
    assert retry_delays == [1]
    assert result.data == {"attempt": 2, "attempts": 2, "duration_ms": 125}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status_code", "location", "redirect_check", "error_type"),
    [
        (200, None, False, None),
        (301, "/ready", True, None),
        (302, "/ready", True, "unexpected_status"),
        (301, "/elsewhere", True, "unexpected_location"),
        (404, None, False, "http_error"),
        (502, None, False, "http_error"),
    ],
)
async def test_close_failure_preserves_admitted_headers(
    status_code: int,
    location: str | None,
    redirect_check: bool,
    error_type: str | None,
) -> None:
    class FailingCloseBody(_UnreadableBody):
        async def aclose(self) -> None:
            await super().aclose()
            raise httpx.ReadError("synthetic private detail")

    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Location": location} if location is not None else {}
        body = FailingCloseBody()
        bodies.append(body)
        return httpx.Response(status_code, headers=headers, stream=body)

    config: dict[str, Any] = {"retries": 1, "retry_delay": 0}
    if redirect_check:
        config.update(
            follow_redirects=False, expected_status=301, expected_location="/ready"
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpCheckExecutor(client=client)
        result = await executor.execute(_build_check(config=config))

    assert result.status == (
        ResultStatus.OK if error_type is None else ResultStatus.ERROR
    )
    assert result.data["status_code"] == status_code
    if redirect_check:
        assert result.data["location"] == location
    assert result.data.get("error_type") == error_type
    assert result.data["cleanup_error_type"] == "response_close_error"
    assert "synthetic private detail" not in str(result.data)
    assert all(body.closed for body in bodies)
    expected_attempt = 2 if status_code == 502 else 1
    assert len(bodies) == result.data["attempt"] == expected_attempt
