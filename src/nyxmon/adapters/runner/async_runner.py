import anyio
from anyio import to_thread

from anyio.from_thread import BlockingPortalProvider
from typing import Iterable, Callable, Set, Optional

import httpx

from .interface import CheckRunner
from .executors import ExecutorRegistry
from .executors.http_executor import HttpCheckExecutor
from .executors.dns_executor import DnsCheckExecutor
from .executors.json_metrics_executor import JsonMetricsExecutor
from .executors.custom_executor import CustomExecutor
from .executors.imap_executor import ImapCheckExecutor
from .executors.smtp_executor import SmtpCheckExecutor
from .executors.tcp_executor import TcpCheckExecutor
from ...domain import Check, Result, CheckType, ResultStatus


class AsyncCheckRunner(CheckRunner):
    def __init__(self, portal_provider: BlockingPortalProvider) -> None:
        self.portal_provider = portal_provider
        self.executor_registry = ExecutorRegistry()
        # Pre-register executors for startup validation
        self._preregister_executors()

    class _NotImplementedExecutor:
        async def execute(self, check: Check):
            return Result(
                check_id=check.check_id,
                status=ResultStatus.ERROR,
                data={
                    "error_type": "not_implemented",
                    "error_msg": f"Executor for '{check.check_type}' not implemented",
                },
            )

        async def aclose(self) -> None:
            return

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

        # Placeholder executors
        not_impl = self._NotImplementedExecutor()
        self.executor_registry.register(CheckType.PING, not_impl)
        self.executor_registry.register(CheckType.CUSTOM, CustomExecutor())

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

                async with anyio.create_task_group() as tg:
                    for check in checks_list:
                        tg.start_soon(self._run_one, check, send_channel)

                # task group finishes -> all _run_one are done
                await send_channel.aclose()
            finally:
                # Clean up executors
                await self.executor_registry.aclose_all()

                # Close HTTP client if we created it
                if http_client is not None:
                    await http_client.aclose()

            async for result in receive_channel:
                yield result

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

        # Placeholder executors
        not_impl = self._NotImplementedExecutor()
        self.executor_registry.register(CheckType.PING, not_impl)
        self.executor_registry.register(CheckType.CUSTOM, CustomExecutor())

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
        # Get executor for check type (raises UnknownCheckTypeError if not found)
        executor = self.executor_registry.get_executor(check.check_type)
        result = await executor.execute(check)
        await send_channel.send(result)
