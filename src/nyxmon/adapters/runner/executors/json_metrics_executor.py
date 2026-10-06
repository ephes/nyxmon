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
from .http_stream import (
    BodyTooLargeError,
    UnsupportedEncodingError,
    read_capped,
    stream_get,
)

# Only encodings ``read_capped`` can inflate with a bounded output size.
ACCEPT_ENCODING = "gzip, deflate"


OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}


# Longest JSON rendering of an ``actual`` value stored in a failure. With
# ``path: "$"`` the value is the whole response document, which would
# otherwise end up in ``check_result.data`` and on the detail page.
MAX_ACTUAL_CHARS = 200

# Returned by ``_resolve_path`` when the path does not exist, so a missing
# field can be told apart from a field that is present with JSON ``null``.
_MISSING: Any = object()


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
                status_code, raw_body = await self._fetch(client, config)
                if status_code >= 400:
                    last_error = self._error(
                        check.check_id, "http_error", f"HTTP {status_code}"
                    )
                    if (
                        self._is_retryable_status(status_code)
                        and attempt < config.retries
                    ):
                        await anyio.sleep(config.retry_delay)
                        continue
                    return last_error
                body = json.loads(raw_body)
            except BodyTooLargeError as err:
                return self._error(check.check_id, "body_too_large", str(err))
            except UnsupportedEncodingError as err:
                return self._error(check.check_id, "unsupported_encoding", str(err))
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
                # Determine highest severity: if any critical failure, use ERROR; else WARNING
                has_critical = any(f.get("severity") == "critical" for f in failures)
                status = ResultStatus.ERROR if has_critical else ResultStatus.WARNING
                # Build concise error message summarizing failures
                error_msg = self._build_failure_summary(failures)
                return Result(
                    check_id=check.check_id,
                    status=status,
                    data={
                        "error_type": "threshold_failed",
                        "error_msg": error_msg,
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

    async def _fetch(
        self, client: httpx.AsyncClient, config: JsonMetricsCheckConfig
    ) -> tuple[int, bytes]:
        """GET ``config.url`` and return the status and the capped body.

        The body of an error response is not read, nor is the body of any
        redirect followed on the way. A body larger than
        ``config.max_body_bytes`` raises :class:`BodyTooLargeError` without
        being buffered in full.
        """
        async with stream_get(
            client,
            config.url,
            timeout=config.timeout,
            follow_redirects=True,
            auth=self._build_auth(config),
            headers={"Accept-Encoding": ACCEPT_ENCODING},
        ) as response:
            status_code = response.status_code
            if status_code >= 400:
                return status_code, b""
            return status_code, await read_capped(response, config.max_body_bytes)

    def _build_auth(self, config: JsonMetricsCheckConfig):
        if config.auth:
            return httpx.BasicAuth(config.auth["username"], config.auth["password"])
        return None

    def _evaluate(self, payload: Any, config: JsonMetricsCheckConfig) -> list[dict]:
        failures: list[dict] = []
        for chk in config.checks:
            actual = self._resolve_path(payload, chk.path)
            if actual is _MISSING:
                # A field the endpoint stopped sending must not satisfy a
                # rule: ``None != "error"`` would otherwise pass.
                failures.append(
                    {
                        "path": chk.path,
                        "op": chk.op,
                        "expected": chk.value,
                        "actual": None,
                        "reason": "path_missing",
                        "severity": chk.severity,
                    }
                )
                continue

            comparator = OPERATORS[chk.op]
            ok = False
            try:
                ok = comparator(actual, chk.value)
            except Exception:
                ok = False

            if not ok:
                failure = {
                    "path": chk.path,
                    "op": chk.op,
                    "expected": chk.value,
                    "actual": actual,
                    "severity": chk.severity,
                }
                failure.update(self._bounded_actual(actual))
                failures.append(failure)
        return failures

    @staticmethod
    def _bounded_actual(actual: Any) -> dict[str, Any]:
        """Return ``actual`` for a failure, cut down if its JSON form is long."""
        rendered = json.dumps(actual, default=str, ensure_ascii=False)
        if len(rendered) <= MAX_ACTUAL_CHARS:
            return {"actual": actual}
        return {
            "actual": rendered[:MAX_ACTUAL_CHARS] + "…",
            "actual_truncated": True,
        }

    def _resolve_path(self, payload: Any, path: str) -> Any:
        """Minimal path resolver for dotted paths like $.a.b.c.

        Returns ``_MISSING`` when the path does not exist in ``payload``.
        """
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
                if not 0 <= idx < len(current):
                    return _MISSING
                current = current[idx]
            else:
                return _MISSING
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

    def _build_failure_summary(self, failures: list[dict]) -> str:
        """Build a concise summary of threshold failures for notifications."""
        if not failures:
            return ""

        # Group by severity
        critical = [f for f in failures if f.get("severity") == "critical"]
        warning = [f for f in failures if f.get("severity") == "warning"]

        parts = []
        if critical:
            paths = [f.get("path", "?") for f in critical]
            parts.append(f"{len(critical)} critical: {', '.join(paths)}")
        if warning:
            paths = [f.get("path", "?") for f in warning]
            parts.append(f"{len(warning)} warning: {', '.join(paths)}")

        return "; ".join(parts)

    def _is_retryable_status(self, status: int) -> bool:
        """Retry on transient HTTP status codes."""
        return 500 <= status < 600
