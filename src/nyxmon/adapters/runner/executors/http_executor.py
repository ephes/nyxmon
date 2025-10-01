"""HTTP check executor implementation."""

import asyncio
import httpx
from typing import Optional

from ....domain import Check, Result, ResultStatus


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
                self._created_client = httpx.AsyncClient(
                    follow_redirects=True, timeout=10
                )

        return self._created_client

    async def execute(self, check: Check) -> Result:
        """Execute an HTTP check and return a Result.

        Args:
            check: The HTTP check to execute

        Returns:
            Result with HTTP response information
        """
        client = await self._get_client()

        try:
            r = await client.get(check.url)
            r.raise_for_status()
            result = Result(check_id=check.check_id, status=ResultStatus.OK, data={})
        except httpx.HTTPStatusError as e:
            result = Result(
                check_id=check.check_id,
                status=ResultStatus.ERROR,
                data={"error_msg": str(e), "status_code": e.response.status_code},
            )
        except httpx.ConnectError as e:
            result = Result(
                check_id=check.check_id,
                status=ResultStatus.ERROR,
                data={"error_type": "connection_error", "error_msg": str(e)},
            )
        except httpx.RequestError as e:
            result = Result(
                check_id=check.check_id,
                status=ResultStatus.ERROR,
                data={"error_type": "request_error", "error_msg": str(e)},
            )
        except Exception as e:
            # Catch-all for any other exceptions
            result = Result(
                check_id=check.check_id,
                status=ResultStatus.ERROR,
                data={"error_msg": str(e)},
            )

        return result

    async def aclose(self) -> None:
        """Close the HTTP client if we created it.

        Only closes the client if this executor created it.
        Externally provided clients are not closed.
        """
        if self._owns_client and self._created_client is not None:
            await self._created_client.aclose()
            self._created_client = None
