import anyio
from anyio import to_thread

from anyio.from_thread import BlockingPortalProvider
from typing import Iterable, Callable

import httpx

from .interface import CheckRunner
from ...domain import Check, Result, ResultStatus


class AsyncCheckRunner(CheckRunner):
    def __init__(self, portal_provider: BlockingPortalProvider) -> None:
        self.portal_provider = portal_provider

    def run_all(self, checks: Iterable[Check], result_received: Callable) -> None:
        """Run all checks."""

        async def run_checks(result_received_callback: Callable) -> None:
            async for result in self._async_run_all(checks):
                # Process the result in a worker thread
                await to_thread.run_sync(result_received_callback, result)

        # Run the async function in the portal
        with self.portal_provider as portal:
            portal.call(run_checks, result_received)

    async def _async_run_all(self, checks: Iterable[Check]):
        send_channel: anyio.abc.ObjectSendStream[Result]
        receive_channel: anyio.abc.ObjectReceiveStream[Result]
        send_channel, receive_channel = anyio.create_memory_object_stream(
            max_buffer_size=100
        )

        async with send_channel, receive_channel:
            async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
                async with anyio.create_task_group() as tg:
                    for check in checks:
                        tg.start_soon(self._run_one, client, check, send_channel)

                # task group finishes -> all _run_one are done
                await send_channel.aclose()

            async for result in receive_channel:
                yield result

    async def _run_one(
        self,
        client: httpx.AsyncClient,
        check: Check,
        send_channel: anyio.abc.ObjectSendStream,
    ) -> None:
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

        await send_channel.send(result)
