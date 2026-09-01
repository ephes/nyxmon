import time
import os
import anyio
import logging
import math
import threading
from functools import lru_cache

from typing import Callable, Protocol
from contextlib import asynccontextmanager

from anyio import to_thread
from anyio.from_thread import BlockingPortalProvider

from ..domain import Auto
from ..domain.commands import (
    AddCheckResult,
    ExecuteChecks,
    StartCollector,
    StopCollector,
)
from ..domain.models import Check, CheckResult, Result, ResultStatus
from ..service_layer import MessageBus

logger = logging.getLogger(__name__)

DEFAULT_PROCESSING_LEASE_SECONDS = 900
RESULT_HANDLING_BUDGET_SECONDS = 60
MAX_DERIVED_PROCESSING_LEASE_SECONDS = 3600
ABANDONED_BATCH_REMINDER_SECONDS = 3600
ABANDONED_BATCH_RETRY_SECONDS = 60


@lru_cache(maxsize=None)
def _processing_lease_seconds_from_value(value: str) -> int:
    if not value:
        return DEFAULT_PROCESSING_LEASE_SECONDS
    try:
        parsed = int(value)
    except ValueError:
        logger.warning(
            "NYXMON_PROCESSING_LEASE_SECONDS is invalid; using default %s",
            DEFAULT_PROCESSING_LEASE_SECONDS,
        )
        return DEFAULT_PROCESSING_LEASE_SECONDS
    if parsed < 30:
        logger.warning("NYXMON_PROCESSING_LEASE_SECONDS is below 30; clamping to 30")
        return 30
    return parsed


def processing_lease_seconds() -> int:
    value = os.environ.get("NYXMON_PROCESSING_LEASE_SECONDS", "").strip()
    return _processing_lease_seconds_from_value(value)


def estimated_check_runtime_seconds(
    check: Check, warning_keys: set[tuple[int, str]] | None = None
) -> int:
    """Conservatively estimate a configured check's legitimate runtime."""
    data = check.data if isinstance(check.data, dict) else {}

    def non_negative_number(name: str, default: float) -> float:
        value = data.get(name, default)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                parsed = float(value)
                if math.isfinite(parsed):
                    return min(
                        max(0.0, parsed),
                        float(MAX_DERIVED_PROCESSING_LEASE_SECONDS + 1),
                    )
            except OverflowError:
                pass
        if isinstance(value, str):
            try:
                parsed = float(value)
                if math.isfinite(parsed):
                    return min(
                        max(0.0, parsed),
                        float(MAX_DERIVED_PROCESSING_LEASE_SECONDS + 1),
                    )
            except (ValueError, OverflowError):
                pass
        warning_key = (check.check_id, name)
        if warning_keys is not None and warning_key not in warning_keys:
            logger.warning(
                "check_id=%s has non-numeric %s; using runtime-estimate default %s",
                check.check_id,
                name,
                default,
            )
            warning_keys.add(warning_key)
        return default

    explicit_budget = non_negative_number("max_runtime_seconds", 0)
    if explicit_budget:
        return math.ceil(explicit_budget)

    retries = math.floor(non_negative_number("retries", 0))
    retry_delay = non_negative_number("retry_delay", 0)
    timeout = non_negative_number("timeout", 10)
    if check.check_type == "imap":
        per_attempt = timeout * 8
    elif check.check_type == "smtp":
        per_attempt = timeout * 5
    elif check.check_type == "tcp":
        per_attempt = (
            non_negative_number("connect_timeout", 10)
            + non_negative_number("tls_handshake_timeout", 10)
            + timeout
        )
    else:
        per_attempt = timeout
    return math.ceil((retries + 1) * per_attempt + retries * retry_delay + 30)


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

    def set_recovery_handler(self, handler: Callable[[AddCheckResult], bool]) -> None:
        """Set the isolated handler used for lease-recovery results."""

    def set_process_notifier(self, notifier: Callable[[Check, Result], None]) -> None:
        """Set the notifier for collector-wide failures without a check row."""


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
        self._last_lease_warning: tuple[int, int] | None = None
        self._last_lease_cap_warning: int | None = None
        self._batch_limiter: anyio.CapacityLimiter | None = None
        self._cached_required_lease = 0
        self._runtime_budget_refresh_at = 0.0
        self._runtime_estimate_warning_keys: set[tuple[int, str]] = set()
        self._abandoned_batch_done: threading.Event | None = None
        self._abandoned_batch_checks: list[Check] = []
        self._abandoned_batch_last_alert_at = 0.0
        self._abandoned_batch_last_attempt_at = 0.0
        self._recovery_handler: Callable[[AddCheckResult], bool] | None = None
        self._process_notifier: Callable[[Check, Result], None] | None = None

    def set_portal_provider(self, portal_provider: BlockingPortalProvider) -> None:
        """Set the portal provider for the collector."""
        self._portal_provider = portal_provider

    def set_message_bus(self, bus: MessageBus) -> None:
        """Set the message bus for the collector."""
        self._bus = bus

    def set_recovery_handler(self, handler: Callable[[AddCheckResult], bool]) -> None:
        """Use a handler with its own unit of work outside a wedged batch."""
        self._recovery_handler = handler

    def set_process_notifier(self, notifier: Callable[[Check, Result], None]) -> None:
        self._process_notifier = notifier

    async def _notify_collector_paused(self) -> bool:
        """Notify without borrowing a live check when all batch rows are gone."""
        if self._process_notifier is None:
            logger.error("collector-wide notifier is not configured")
            return False
        check = Check(
            check_id=0,
            service_id=0,
            name="Nyxmon collector",
            check_type="internal",
            url="internal://collector",
            data={},
        )
        result = Result(
            check_id=0,
            status=ResultStatus.ERROR,
            data={
                "error_type": "collector_execution_paused",
                "error_msg": (
                    "All new check executions are paused because an abandoned "
                    "batch thread is still running"
                ),
            },
        )
        try:
            await to_thread.run_sync(
                self._process_notifier,
                check,
                result,
                abandon_on_cancel=False,
            )
            return True
        except Exception:
            logger.exception("failed to send collector-wide paused notification")
            return False

    async def _record_stale_check(
        self, check: Check, *, force_notification: bool = False
    ) -> bool:
        """Record one expired lease without blocking recovery of its peers."""
        try:
            error_type = (
                "collector_execution_paused"
                if force_notification
                else "stale_processing_lease"
            )
            error_msg = (
                "All new check executions are paused because an abandoned "
                "batch thread is still running"
                if force_notification
                else (
                    "Check processing lease expired; the previous worker "
                    "did not persist a result"
                )
            )
            stale_result = Result(
                check_id=check.check_id,
                status=ResultStatus.ERROR,
                data={
                    "error_type": error_type,
                    "error_msg": error_msg,
                    "notification_immediate": True,
                },
            )
            if not force_notification:
                check.schedule_next_check()
            # A production bootstrap supplies an independently injected handler
            # with its own UnitOfWork. It must not share transaction state with
            # an abandoned ExecuteChecks handler that may still be running.
            handler = self._recovery_handler
            if handler is None:
                raise RuntimeError("lease recovery handler is not configured")
            notification_attempted = await to_thread.run_sync(
                handler,
                AddCheckResult(
                    check_result=CheckResult(
                        check=check,
                        result=stale_result,
                        force_notification=force_notification,
                    )
                ),
                abandon_on_cancel=False,
            )
            return bool(notification_attempted)
        except Exception:
            logger.exception(
                "failed to record expired processing lease for check_id=%s",
                check.check_id,
            )
            return False

    async def _collect_once(self) -> None:
        effective_lease = processing_lease_seconds()
        configured_lease = effective_lease
        try:
            now = anyio.current_time()
            if now >= self._runtime_budget_refresh_at:
                all_checks = await self._bus.uow.store.checks.list_async()
                estimated_required_lease = max(
                    (
                        estimated_check_runtime_seconds(
                            check, self._runtime_estimate_warning_keys
                        )
                        for check in all_checks
                        if not check.disabled
                    ),
                    default=0,
                )
                if estimated_required_lease > MAX_DERIVED_PROCESSING_LEASE_SECONDS:
                    if self._last_lease_cap_warning != estimated_required_lease:
                        logger.warning(
                            "estimated maximum check runtime %ss exceeds the "
                            "derived lease ceiling %ss; clamping the estimate",
                            estimated_required_lease,
                            MAX_DERIVED_PROCESSING_LEASE_SECONDS,
                        )
                    self._last_lease_cap_warning = estimated_required_lease
                else:
                    self._last_lease_cap_warning = None
                self._cached_required_lease = min(
                    estimated_required_lease,
                    MAX_DERIVED_PROCESSING_LEASE_SECONDS,
                )
                refresh_interval = max(5.0, min(30.0, configured_lease / 2))
                self._runtime_budget_refresh_at = now + refresh_interval
            required_lease = self._cached_required_lease
            effective_lease = max(configured_lease, required_lease)
            warning_key = (configured_lease, required_lease)
            if required_lease > configured_lease:
                if self._last_lease_warning != warning_key:
                    logger.warning(
                        "configured processing lease %ss is below the estimated "
                        "maximum check runtime %ss; using %ss",
                        configured_lease,
                        required_lease,
                        effective_lease,
                    )
                self._last_lease_warning = warning_key
            else:
                self._last_lease_warning = None
        except Exception:
            logger.exception(
                "processing-lease runtime estimation failed; using the last safe budget"
            )
        effective_lease = max(configured_lease, self._cached_required_lease)
        try:
            stale_checks = await self._bus.uow.store.checks.reclaim_stale_checks_async(
                effective_lease
            )
        except Exception:
            logger.exception("processing-lease reclaim failed")
            stale_checks = []

        for check in stale_checks:
            if not check.disabled:
                await self._record_stale_check(check)

        if self._abandoned_batch_done is not None:
            if not self._abandoned_batch_done.is_set():
                now = anyio.current_time()
                if (
                    now - self._abandoned_batch_last_alert_at
                    >= ABANDONED_BATCH_REMINDER_SECONDS
                    and now - self._abandoned_batch_last_attempt_at
                    >= ABANDONED_BATCH_RETRY_SECONDS
                ):
                    logger.error(
                        "abandoned check batch is still running; new execution "
                        "batches remain paused"
                    )
                    current_checks = await self._bus.uow.store.checks.list_async()
                    abandoned_ids = {
                        check.check_id for check in self._abandoned_batch_checks
                    }
                    abandoned_check = next(
                        (
                            check
                            for check in current_checks
                            if check.check_id in abandoned_ids
                        ),
                        None,
                    )
                    self._abandoned_batch_last_attempt_at = now
                    notification_attempted = False
                    if abandoned_check is not None:
                        notification_attempted = await self._record_stale_check(
                            abandoned_check,
                            force_notification=True,
                        )
                    else:
                        notification_attempted = await self._notify_collector_paused()
                    if notification_attempted:
                        self._abandoned_batch_last_alert_at = now
                return
            self._abandoned_batch_done = None
            self._abandoned_batch_checks = []

        checks = await self._bus.uow.store.checks.list_due_checks_async()
        logger.debug("due checks: %s", checks)
        if checks:
            # Abandon the wait after the effective lease so this collector can
            # keep reaping and scheduling even if an executor thread wedges.
            if self._batch_limiter is None:
                self._batch_limiter = anyio.CapacityLimiter(1)
            result_handling_budget = len(checks) * RESULT_HANDLING_BUDGET_SECONDS
            batch_deadline = effective_lease + result_handling_budget
            batch_done = threading.Event()

            def run_batch() -> None:
                try:
                    self._bus.handle(ExecuteChecks(checks=checks))
                finally:
                    batch_done.set()

            with anyio.move_on_after(batch_deadline) as cancel_scope:
                await to_thread.run_sync(
                    run_batch,
                    abandon_on_cancel=True,
                    limiter=self._batch_limiter,
                )
            if cancel_scope.cancel_called:
                self._abandoned_batch_done = batch_done
                self._abandoned_batch_checks = checks
                abandoned_at = anyio.current_time()
                self._abandoned_batch_last_alert_at = abandoned_at
                self._abandoned_batch_last_attempt_at = abandoned_at
                logger.error(
                    "check batch exceeded its %ss execution/result deadline; "
                    "new executions are paused while lease recovery and alerts continue",
                    batch_deadline,
                )

    async def _async_start(self):
        if self._running:
            return
        if self._bus is None:
            raise ValueError(
                "Message bus is not set. Please set the message bus before starting the collector."
            )
        self._running = True
        while self._running:
            try:
                await self._collect_once()
            except Exception:
                logger.exception("check collector iteration failed")
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
