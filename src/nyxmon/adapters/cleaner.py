import time
import anyio
import logging
import threading

from typing import Protocol
from contextlib import asynccontextmanager

from anyio.from_thread import BlockingPortalProvider

from ..domain import Auto
from ..domain.commands import StartCleaner, StopCleaner
from ..adapters.repositories import RepositoryStore

logger = logging.getLogger(__name__)


class ResultsCleaner(Protocol):
    """A protocol for a results cleaner."""

    def __init__(
        self,
        *,
        interval: int = 3600,
        retention_period: int = 86400,
        batch_size: int = 1000,
    ) -> None: ...

    def start(self) -> None:
        """Start the cleaner."""
        ...

    def stop(self) -> None:
        """Stop the cleaner."""
        ...

    def set_portal_provider(self, portal_provider) -> None:
        """Set the portal provider for the cleaner."""
        pass

    def set_store(self, store: RepositoryStore) -> None:
        """Set the repository store for the cleaner."""
        pass


@asynccontextmanager
async def running_cleaner(bus):
    """Context manager for cleaner lifecycle"""
    bus.handle(StartCleaner())
    try:
        yield
    finally:
        bus.handle(StopCleaner())
        # Optional: wait a bit for cleaner to shut down cleanly
        await anyio.sleep(0.1)


class AsyncResultsCleaner(ResultsCleaner):
    def __init__(
        self,
        *,
        interval: int = 3600,
        retention_period: int = 86400,
        batch_size: int = 1000,
    ) -> None:
        self.interval = interval
        self.retention_period = retention_period
        self.batch_size = batch_size
        self._running = False
        self._thread = Auto
        self._store = Auto
        self._portal_provider = Auto

    def set_portal_provider(self, portal_provider: BlockingPortalProvider) -> None:
        """Set the portal provider for the cleaner."""
        self._portal_provider = portal_provider

    def set_store(self, store: RepositoryStore) -> None:
        """Set the repository store for the cleaner."""
        self._store = store

    async def _async_start(self):
        if self._running:
            return
        if self._store is None:
            raise ValueError(
                "Repository store is not set. Please set the store before starting the cleaner."
            )
        self._running = True

        while self._running:
            try:
                # Delete old results
                deleted_count = await self._store.results.delete_old_results_async(
                    retention_seconds=self.retention_period, batch_size=self.batch_size
                )
                if deleted_count > 0:
                    logger.info(f"Cleaned up {deleted_count} old check results")
            except Exception as e:
                logger.exception(f"Error during result cleanup: {e}")

            # Sleep until next cleanup cycle
            await anyio.sleep(self.interval)

    def start(self) -> None:
        thread = threading.Thread(
            target=self._start_in_thread,
            daemon=True,  # Make it a daemon thread so it doesn't block program exit
        )
        thread.start()
        self._thread = thread
        logger.debug("results cleaner started!")

    def _start_in_thread(self) -> None:
        """Run the cleaner in a thread."""
        with self._portal_provider as portal:
            portal.start_task_soon(self._async_start)
            # This thread will keep running as long as the portal is alive
            # Keep thread alive but don't consume CPU
            while self._running:
                time.sleep(1)

    def stop(self):
        if not self._running:
            return

        self._running = False

        # Wait for the thread to finish if it exists
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)  # Wait up to 2 seconds

        # Log or handle if thread didn't exit cleanly
        if self._thread and self._thread.is_alive():
            logger.warning("Warning: Cleaner thread didn't exit cleanly")
        logger.debug("results cleaner stopped!")
