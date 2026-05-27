"""HTTP check executor implementation."""

import asyncio
import time
from typing import Any, Optional

import anyio
import httpx

from ....domain import Check, Result, ResultStatus
from ....domain.http_config import HttpCheckConfig


class HttpCheckExecutor:
    """Executor for HTTP checks.

    Performs HTTP requests and validates responses.
    Can accept an external client or create its own.
    """

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        """Initialize HTTP executor.

        Args:
            client: Optional shared httpx client. If None, creates its own.
        """
        self._client = client
        self._owns_client = client is None
        self._created_client: Optional[httpx.AsyncClient] = None
        self._client_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client.

        Thread-safe creation of HTTP client for concurrent check execution.

        Returns:
            HTTP client instance
        """
        if self._client is not None:
            return self._client

        # Protect client creation from concurrent access
        async with self._client_lock:
            if self._created_client is None:
                self._created_client = httpx.AsyncClient(follow_redirects=True)

        return self._created_client

    async def execute(self, check: Check) -> Result:
        """Execute an HTTP check and return a Result.

        Args:
            check: The HTTP check to execute

        Returns:
            Result with HTTP response information
        """
        try:
            config = HttpCheckConfig.from_dict(check.data)
            config.validate()
        except ValueError as exc:
            return self._error(
                check.check_id,
                "configuration_error",
                str(exc),
                {"attempt": 0, "attempts": 0},
            )

        client = await self._get_client()
        attempts = config.retries + 1

        for attempt in range(1, attempts + 1):
            start = time.time()
            try:
                response = await client.get(check.url, timeout=config.timeout)
                status_code = self._response_status_code(response)
                if status_code is not None and not 200 <= status_code < 300:
                    result = await self._http_status_error_result(
                        check.check_id,
                        status_code,
                        attempt,
                        attempts,
                        config,
                    )
                    if result is None:
                        continue
                    return result
                if status_code is None:
                    response.raise_for_status()

                duration_ms = int((time.time() - start) * 1000)
                data: dict[str, Any] = {}
                if attempt > 1 or attempts > 1:
                    data.update(
                        {
                            "attempt": attempt,
                            "attempts": attempts,
                            "duration_ms": duration_ms,
                        }
                    )
                return Result(
                    check_id=check.check_id,
                    status=ResultStatus.OK,
                    data=data,
                )
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                result = await self._http_status_error_result(
                    check.check_id,
                    status_code,
                    attempt,
                    attempts,
                    config,
                )
                if result is None:
                    continue
                return result
            except httpx.TimeoutException as exc:
                error_data = {
                    "error_type": "timeout",
                    "error_msg": str(exc),
                    "attempt": attempt,
                    "attempts": attempts,
                }
                if attempt < attempts:
                    await anyio.sleep(config.retry_delay)
                    continue
                return Result(
                    check_id=check.check_id,
                    status=ResultStatus.ERROR,
                    data=error_data,
                )
            except httpx.ConnectError as exc:
                error_data = {
                    "error_type": "connection_error",
                    "error_msg": str(exc),
                    "attempt": attempt,
                    "attempts": attempts,
                }
                if attempt < attempts:
                    await anyio.sleep(config.retry_delay)
                    continue
                return Result(
                    check_id=check.check_id,
                    status=ResultStatus.ERROR,
                    data=error_data,
                )
            except httpx.RequestError as exc:
                error_data = {
                    "error_type": "request_error",
                    "error_msg": str(exc),
                    "attempt": attempt,
                    "attempts": attempts,
                }
                if attempt < attempts:
                    await anyio.sleep(config.retry_delay)
                    continue
                return Result(
                    check_id=check.check_id,
                    status=ResultStatus.ERROR,
                    data=error_data,
                )
            except Exception as exc:  # noqa: BLE001
                return self._error(
                    check.check_id,
                    "unexpected_error",
                    str(exc),
                    {"attempt": attempt, "attempts": attempts},
                )

        return self._error(
            check.check_id,
            "unexpected_error",
            "HTTP check failed",
            {"attempt": attempts, "attempts": attempts},
        )

    def _response_status_code(self, response: httpx.Response) -> int | None:
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            return status_code
        return None

    async def _http_status_error_result(
        self,
        check_id: int,
        status_code: int,
        attempt: int,
        attempts: int,
        config: HttpCheckConfig,
    ) -> Result | None:
        error_data = {
            "error_type": "http_error",
            "error_msg": f"HTTP {status_code}",
            "status_code": status_code,
            "attempt": attempt,
            "attempts": attempts,
        }
        if status_code in config.retry_status_codes and attempt < attempts:
            await anyio.sleep(config.retry_delay)
            return None
        return Result(
            check_id=check_id,
            status=ResultStatus.ERROR,
            data=error_data,
        )

    def _error(
        self, check_id: int, error_type: str, msg: str, data: dict[str, Any]
    ) -> Result:
        payload: dict[str, Any] = {"error_type": error_type, "error_msg": msg}
        payload.update(data)
        return Result(check_id=check_id, status=ResultStatus.ERROR, data=payload)

    async def aclose(self) -> None:
        """Close the HTTP client if we created it.

        Only closes the client if this executor created it.
        Externally provided clients are not closed.
        """
        if self._owns_client and self._created_client is not None:
            await self._created_client.aclose()
            self._created_client = None
