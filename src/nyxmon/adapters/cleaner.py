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

# Upper bound on batches per cleanup cycle. With the default batch size this
# drains up to 100,000 rows per cycle (2.4 million a day at the default
# interval); a larger backlog continues on the next cycle.
DEFAULT_MAX_BATCHES_PER_CYCLE = 100


class ResultsCleaner(Protocol):
    """A protocol for a results cleaner."""

    def __init__(
        self,
        *,
        interval: int = 3600,
        retention_period: int = 86400,
        batch_size: int = 1000,
        max_batches_per_cycle: int = DEFAULT_MAX_BATCHES_PER_CYCLE,
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
        max_batches_per_cycle: int = DEFAULT_MAX_BATCHES_PER_CYCLE,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if max_batches_per_cycle < 1:
            raise ValueError("max_batches_per_cycle must be at least 1")
        self.interval = interval
        self.retention_period = retention_period
        self.batch_size = batch_size
        self.max_batches_per_cycle = max_batches_per_cycle
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
                await self.run_cleanup_cycle()
            except Exception as e:
                logger.exception(f"Error during result cleanup: {e}")

            # Sleep until next cleanup cycle
            await anyio.sleep(self.interval)

    async def run_cleanup_cycle(self) -> int:
        """Delete expired results batch by batch until none are left.

        Each batch is its own short transaction, and the loop yields between
        batches so the collector can write results in between. The loop stops
        after ``max_batches_per_cycle`` batches, so one cycle never holds the
        database for long; any remaining backlog is drained next cycle.
        Returns the total number of rows deleted in this cycle.
        """
        total_deleted = 0
        batches = 0
        drained = False
        while batches < self.max_batches_per_cycle:
            deleted = await self._store.results.delete_old_results_async(
                retention_seconds=self.retention_period, batch_size=self.batch_size
            )
            batches += 1
            total_deleted += deleted
            if deleted < self.batch_size:
                drained = True
                break
            await anyio.sleep(0)

        if total_deleted > 0:
            logger.info(
                "Cleaned up %d old check results in %d batch(es)",
                total_deleted,
                batches,
            )
        if not drained:
            logger.warning(
                "Result cleanup stopped after %d batches (%d rows); "
                "the remaining backlog is deleted next cycle",
                batches,
                total_deleted,
            )
        return total_deleted

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
