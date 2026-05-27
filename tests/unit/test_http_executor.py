"""Unit tests for the plain HTTP check executor."""

from __future__ import annotations

from typing import Any

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

    async def get(self, url: str, timeout: float | None = None) -> Any:
        del url
        self.calls += 1
        self.timeouts.append(timeout)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def aclose(self) -> None:
        return None


class StubResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


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
