import time
import anyio
import logging
import threading

from typing import Protocol
from contextlib import asynccontextmanager

from anyio import to_thread
from anyio.from_thread import BlockingPortalProvider

from ..domain import Auto
from ..domain.commands import ExecuteChecks, StartCollector, StopCollector
from ..service_layer import MessageBus

logger = logging.getLogger(__name__)


class CheckCollector(Protocol):
    """A protocol for a check collector."""

    def __init__(self, *, interval: int = 1) -> None: ...

    def start(self) -> None:
        """Start the collector."""
        ...

    def stop(self) -> None:
        """Stop the collector."""
        ...

    def set_portal_provider(self, portal_provider) -> None:
        """Set the portal provider for the collector."""
        pass

    def set_message_bus(self, bus: MessageBus) -> None:
        """Set the message bus for the collector."""


@asynccontextmanager
async def running_collector(bus):
    """Context manager for collector lifecycle"""
    bus.handle(StartCollector())
    try:
        yield
    finally:
        bus.handle(StopCollector())
        # Optional: wait a bit for collector to shut down cleanly
        await anyio.sleep(0.1)


class AsyncCheckCollector(CheckCollector):
    def __init__(self, *, interval: int = 1) -> None:
        self.interval = interval
        self._running = False
        self._thread = Auto
        self._bus = Auto

    def set_portal_provider(self, portal_provider: BlockingPortalProvider) -> None:
        """Set the portal provider for the collector."""
        self._portal_provider = portal_provider

    def set_message_bus(self, bus: MessageBus) -> None:
        """Set the message bus for the collector."""
        self._bus = bus

    async def _async_start(self):
        if self._running:
            return
        if self._bus is None:
            raise ValueError(
                "Message bus is not set. Please set the message bus before starting the collector."
            )
        self._running = True
        i = 0
        while self._running:
            checks = await self._bus.uow.store.checks.list_due_checks_async()
            print("due checks: ", checks)
            if len(checks) > 0:
                # Use a worker thread to run the checks
                await to_thread.run_sync(self._bus.handle, ExecuteChecks(checks=checks))
            i += 1
            await anyio.sleep(self.interval)

    def start(self) -> None:
        thread = threading.Thread(
            target=self._start_in_thread,
            daemon=True,  # Make it a daemon thread so it doesn't block program exit
        )
        thread.start()
        self._thread = thread
        logger.debug("check collector started!")

    def _start_in_thread(self) -> None:
        """Run the collector in a thread."""
        with self._portal_provider as portal:
            portal.start_task_soon(self._async_start)
            # This thread will keep running as long as the portal is alive
            # Add some way to join/exit this thread when needed
            while self._running:
                time.sleep(1)  # Keep thread alive but don't consume CPU

    def stop(self):
        if not self._running:
            return

        self._running = False

        # Wait for the thread to finish if it exists
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)  # Wait up to 2 seconds

        # Log or handle if thread didn't exit cleanly
        if self._thread and self._thread.is_alive():
            logger.warning("Warning: Collector thread didn't exit cleanly")
        logger.debug("check collector stopped!")
