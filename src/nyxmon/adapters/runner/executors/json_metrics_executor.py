"""JSON metrics check executor implementation."""

from __future__ import annotations

import json
import anyio
import operator
import re
import time
from typing import Any, Callable, Optional

import httpx

from ....domain import Check, Result, ResultStatus
from ....domain.json_metrics_config import JsonMetricsCheckConfig


OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}


class JsonMetricsError(Exception):
    """Base error for JSON metrics executor."""  # pragma: no cover - base class only


class JsonMetricsExecutor:
    """Executor for JSON metrics checks."""

    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        self._client = client
        self._owns_client = client is None
        self._created_client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client:
            return self._client

        if self._created_client is None:
            self._created_client = httpx.AsyncClient(follow_redirects=True)
        return self._created_client

    async def execute(self, check: Check) -> Result:
        try:
            config = JsonMetricsCheckConfig.from_dict(check.data)
            config.validate()
        except ValueError as err:
            return self._error(check.check_id, "configuration_error", str(err))

        client = await self._get_client()
        attempts = config.retries + 1
        last_error: Result | None = None

        for attempt in range(attempts):
            start = time.time()
            try:
                response = await client.get(
                    config.url,
                    timeout=config.timeout,
                    auth=self._build_auth(config),
                )
                if response.status_code >= 400:
                    last_error = self._error(
                        check.check_id, "http_error", f"HTTP {response.status_code}"
                    )
                    if (
                        self._is_retryable_status(response.status_code)
                        and attempt < config.retries
                    ):
                        await anyio.sleep(config.retry_delay)
                        continue
                    return last_error
                body = response.json()
            except httpx.TimeoutException as err:
                last_error = self._error(check.check_id, "timeout", str(err))
                if attempt < config.retries:
                    await anyio.sleep(config.retry_delay)
                    continue
                return last_error
            except httpx.RequestError as err:
                last_error = self._error(check.check_id, "request_error", str(err))
                if attempt < config.retries:
                    await anyio.sleep(config.retry_delay)
                    continue
                return last_error
            except (json.JSONDecodeError, ValueError) as err:
                return self._error(check.check_id, "json_error", str(err))
            except Exception as err:  # noqa: BLE001
                return self._error(check.check_id, "unexpected_error", str(err))

            duration_ms = int((time.time() - start) * 1000)
            failures = self._evaluate(body, config)
            if failures:
                return Result(
                    check_id=check.check_id,
                    status=ResultStatus.ERROR,
                    data={
                        "error_type": "threshold_failed",
                        "failures": failures,
                        "duration_ms": duration_ms,
                    },
                )

            return Result(
                check_id=check.check_id,
                status=ResultStatus.OK,
                data={"duration_ms": duration_ms},
            )

        return last_error or self._error(
            check.check_id, "unexpected_error", "metrics check failed"
        )

    def _build_auth(self, config: JsonMetricsCheckConfig):
        if config.auth:
            return httpx.BasicAuth(config.auth["username"], config.auth["password"])
        return None

    def _evaluate(self, payload: Any, config: JsonMetricsCheckConfig) -> list[dict]:
        failures: list[dict] = []
        for chk in config.checks:
            actual = self._resolve_path(payload, chk.path)
            comparator = OPERATORS[chk.op]
            ok = False
            try:
                ok = comparator(actual, chk.value)
            except Exception:
                ok = False

            if not ok:
                failures.append(
                    {
                        "path": chk.path,
                        "op": chk.op,
                        "expected": chk.value,
                        "actual": actual,
                        "severity": chk.severity,
                    }
                )
        return failures

    def _resolve_path(self, payload: Any, path: str) -> Any:
        """Minimal path resolver for dotted paths like $.a.b.c."""
        if path == "$":
            return payload

        normalized = re.sub(r"\[(\d+)\]", r".\1", path)
        parts = [p for p in normalized.replace("$.", "").split(".") if p]
        current = payload
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, list) and part.isdigit():
                idx = int(part)
                current = current[idx] if 0 <= idx < len(current) else None
            else:
                return None
        return current

    async def aclose(self) -> None:
        if self._owns_client and self._created_client:
            await self._created_client.aclose()
            self._created_client = None

    def _error(self, check_id: int, error_type: str, msg: str) -> Result:
        return Result(
            check_id=check_id,
            status=ResultStatus.ERROR,
            data={"error_type": error_type, "error_msg": msg},
        )

    def _is_retryable_status(self, status: int) -> bool:
        """Retry on transient HTTP status codes."""
        return 500 <= status < 600
