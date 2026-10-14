import anyio
import logging
from anyio import to_thread

from anyio.from_thread import BlockingPortalProvider
from typing import Iterable, Callable, Set, Optional, Awaitable, cast

import httpx

from .interface import CheckRunner
from .executors import ExecutorRegistry, UnknownCheckTypeError
from .executors.http_executor import HttpCheckExecutor
from .executors.dns_executor import DnsCheckExecutor
from .executors.json_metrics_executor import JsonMetricsExecutor
from .executors.imap_executor import ImapCheckExecutor
from .executors.smtp_executor import SmtpCheckExecutor
from .executors.tcp_executor import TcpCheckExecutor
from .executors.ping_executor import PingCheckExecutor
from ...domain import Check, Result, CheckType, ResultStatus
from ...domain.commands import IntakeStop

logger = logging.getLogger(__name__)

# How often a running batch looks for a shutdown intake stop.
INTAKE_STOP_POLL_SECONDS = 0.02
# Queued behind every result that finished before an intake stop.
_INTAKE_STOPPED = object()


class AsyncCheckRunner(CheckRunner):
    def __init__(self, portal_provider: BlockingPortalProvider) -> None:
        self.portal_provider = portal_provider
        self.executor_registry = ExecutorRegistry()
        # Pre-register executors for startup validation
        self._preregister_executors()

    def _preregister_executors(self) -> None:
        """Pre-register executors without HTTP client for startup validation.

        This allows checking registered types before any checks are executed.
        Executors will be re-registered with proper resources during batch execution.
        """
        # Register HTTP executor without client (will create its own if needed)
        http_executor = HttpCheckExecutor(None)
        self.executor_registry.register(CheckType.HTTP, http_executor)
        self.executor_registry.register(CheckType.JSON_HTTP, http_executor)

        # Register DNS executor
        dns_executor = DnsCheckExecutor()
        self.executor_registry.register(CheckType.DNS, dns_executor)

        # Register TCP executor
        tcp_executor = TcpCheckExecutor()
        self.executor_registry.register(CheckType.TCP, tcp_executor)

        # Register JSON metrics executor (shares HTTP client when available)
        json_executor = JsonMetricsExecutor(None)
        self.executor_registry.register(CheckType.JSON_METRICS, json_executor)

        # Register IMAP executor
        imap_executor = ImapCheckExecutor()
        self.executor_registry.register(CheckType.IMAP, imap_executor)

        # Register SMTP executor
        smtp_executor = SmtpCheckExecutor()
        self.executor_registry.register(CheckType.SMTP, smtp_executor)

        # Register ping executor (system ping binary, no raw sockets)
        ping_executor = PingCheckExecutor()
        self.executor_registry.register(CheckType.PING, ping_executor)

    def run_all(
        self,
        checks: Iterable[Check],
        result_received: Callable,
        intake_stop: IntakeStop | None = None,
    ) -> None:
        """Run all checks."""

        async def receive(result: Result) -> None:
            await to_thread.run_sync(result_received, result)

        with self.portal_provider as portal:
            portal.call(self._async_run_batch, checks, receive, intake_stop)

    async def _async_run_all(self, checks: Iterable[Check]):
        """Collect a complete batch for callers using the iterator helper.

        Production uses the callback path to persist results immediately.
        Yield only after the task group closes, preserving cancel-scope nesting.
        """
        results: list[Result] = []

        async def receive(result: Result) -> None:
            results.append(result)

        await self._async_run_batch(checks, receive)
        for result in results:
            yield result

    async def _async_run_batch(
        self,
        checks: Iterable[Check],
        receive: Callable[[Result], Awaitable[None]],
        intake_stop: IntakeStop | None = None,
    ) -> None:
        send_channel: anyio.abc.ObjectSendStream[Result | object]
        receive_channel: anyio.abc.ObjectReceiveStream[Result | object]
        send_channel, receive_channel = anyio.create_memory_object_stream(
            max_buffer_size=100
        )

        # Convert checks to list for pre-scan
        checks_list = list(checks)

        # Pre-scan to determine which check types are present
        check_types = self._scan_check_types(checks_list)

        # Determine if we need an HTTP client
        needs_http_client = bool(
            check_types & {CheckType.HTTP, CheckType.JSON_HTTP, CheckType.JSON_METRICS}
        )

        async with send_channel, receive_channel:
            # Only create HTTP client if needed
            http_client: Optional[httpx.AsyncClient] = None
            if needs_http_client:
                http_client = httpx.AsyncClient(follow_redirects=True, timeout=10)

            try:
                # Register executors with batch context
                self._register_executors(http_client)

                checks_scope = anyio.CancelScope()
                watch_scope = anyio.CancelScope()

                async def produce_results() -> None:
                    with checks_scope:
                        async with anyio.create_task_group() as checks_group:
                            for check in checks_list:
                                checks_group.start_soon(
                                    self._run_one, check, send_channel
                                )
                    await send_channel.aclose()

                async def stop_intake_when_requested(stop: IntakeStop) -> None:
                    with watch_scope:
                        while not stop.requested.is_set():
                            await anyio.sleep(INTAKE_STOP_POLL_SECONDS)
                        checks_scope.cancel()
                        # Results that finished before the request are ahead
                        # of the marker in the queue; later ones are dropped
                        # and their claims released by the collector.
                        try:
                            await send_channel.send(_INTAKE_STOPPED)
                        except anyio.ClosedResourceError:
                            pass  # every check finished, nothing to stop

                # Consume results as they arrive, so one slow check cannot
                # keep completed samples unpersisted until shutdown cancels it.
                async with anyio.create_task_group() as tg:
                    tg.start_soon(produce_results)
                    if intake_stop is not None:
                        tg.start_soon(stop_intake_when_requested, intake_stop)
                    async for result in receive_channel:
                        if result is _INTAKE_STOPPED:
                            break
                        await receive(cast(Result, result))
                    watch_scope.cancel()
                    if intake_stop is not None and intake_stop.requested.is_set():
                        # Checks cancelled at the intake stop may still be
                        # unwinding (an executor thread cannot be interrupted),
                        # so report the hand-over before waiting for them.
                        intake_stop.drained.set()
            finally:
                # Cancellation must not skip async resource cleanup. Bound it
                # too, since a custom executor may itself hang while closing.
                with anyio.move_on_after(1, shield=True):
                    try:
                        await self.executor_registry.aclose_all()
                    finally:
                        if http_client is not None:
                            await http_client.aclose()

    def _scan_check_types(self, checks: list[Check]) -> Set[str]:
        """Scan checks to determine which check types are present.

        Args:
            checks: List of checks to scan

        Returns:
            Set of check types found in the batch
        """
        return {check.check_type for check in checks}

    def _register_executors(self, http_client: Optional[httpx.AsyncClient]) -> None:
        """Register all available executors.

        Args:
            http_client: Optional HTTP client to share with HTTP executor
        """
        # Register HTTP executor (with or without shared client)
        http_executor = HttpCheckExecutor(http_client)
        self.executor_registry.register(CheckType.HTTP, http_executor)
        self.executor_registry.register(
            CheckType.JSON_HTTP, http_executor
        )  # JSON_HTTP uses same executor

        # Register DNS executor
        dns_executor = DnsCheckExecutor()
        self.executor_registry.register(CheckType.DNS, dns_executor)

        # Register TCP executor
        tcp_executor = TcpCheckExecutor()
        self.executor_registry.register(CheckType.TCP, tcp_executor)

        # Register JSON metrics executor (reuse http client)
        json_executor = JsonMetricsExecutor(http_client)
        self.executor_registry.register(CheckType.JSON_METRICS, json_executor)

        # Register IMAP executor
        imap_executor = ImapCheckExecutor()
        self.executor_registry.register(CheckType.IMAP, imap_executor)

        # Register SMTP executor
        smtp_executor = SmtpCheckExecutor()
        self.executor_registry.register(CheckType.SMTP, smtp_executor)

        # Register ping executor (system ping binary, no raw sockets)
        ping_executor = PingCheckExecutor()
        self.executor_registry.register(CheckType.PING, ping_executor)

    async def _run_one(
        self,
        check: Check,
        send_channel: anyio.abc.ObjectSendStream,
    ) -> None:
        """Run a single check.

        Args:
            check: Check to execute
            send_channel: Channel to send result to
        """
        try:
            # Get executor for check type (raises UnknownCheckTypeError if not found)
            executor = self.executor_registry.get_executor(check.check_type)
            result = await executor.execute(check)
        except UnknownCheckTypeError as e:
            # Handle legacy/unknown check types gracefully instead of crashing
            result = Result(
                check_id=check.check_id,
                status=ResultStatus.ERROR,
                data={
                    "error_type": "unknown_check_type",
                    "error_msg": str(e),
                    "check_type": check.check_type,
                },
            )
        except Exception as e:
            logger.exception(
                "check executor raised unexpectedly for check_id=%s check_type=%s",
                check.check_id,
                check.check_type,
            )
            result = Result(
                check_id=check.check_id,
                status=ResultStatus.ERROR,
                data={
                    "error_type": "executor_exception",
                    "error_msg": type(e).__name__,
                    "check_type": check.check_type,
                },
            )
        await send_channel.send(result)
