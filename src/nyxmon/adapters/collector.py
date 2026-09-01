import time
import os
import anyio
import logging
import math
import threading
from functools import lru_cache
from time import time as current_epoch

from typing import Any, Callable, Protocol
from contextlib import asynccontextmanager

from anyio import to_thread
from anyio.from_thread import BlockingPortalProvider

from .repositories.in_memory import InMemoryStore
from .repositories.interface import CollectorIncident, CollectorIncidentAlert
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

# Collector-level incidents are keyed by kind, not by batch: a wedged executor
# or a burst of expired leases is ONE incident that is deduplicated across
# collector iterations and across process restarts, and reminded about on
# elapsed wall-clock time.
COLLECTOR_STALE_BATCH_INCIDENT_KEY = "collector:stale_processing_lease"
COLLECTOR_PAUSED_INCIDENT_KEY = "collector:execution_paused"

# Elapsed-time reminder for an ongoing stale-lease incident. Sample counts are
# deliberately not used: they mean a different interval for every check.
STALE_BATCH_REMINDER_SECONDS = 3600
# Quiet period before a stale-lease incident is considered resolved. A restart
# storm spans several collector iterations, so a single quiet iteration must
# not close the incident and let the next iteration alert again.
STALE_BATCH_RESOLVE_AFTER_SECONDS = 900
# Bounds on what an incident payload/message may carry, so a large batch can
# never produce an unbounded notification.
INCIDENT_CHECK_SAMPLE_LIMIT = 20
INCIDENT_MESSAGE_ID_LIMIT = 10
# Reclaim is bounded per call, so a restart storm spans several calls. Draining
# them inside one iteration recovers the whole batch at once and lets the single
# incident alert report the real size instead of one page-sized slice of it.
MAX_STALE_RECLAIM_ROUNDS = 20


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


class CollectorIncidentStore(Protocol):
    """The persistence surface a collector needs for its own incidents."""

    def get_collector_incident(self, incident_key: str) -> CollectorIncident | None: ...

    def claim_collector_incident_alert(
        self,
        incident_key: str,
        *,
        now: int,
        reminder_seconds: int,
        payload: dict[str, Any] | None = None,
    ) -> CollectorIncidentAlert: ...

    def close_collector_incident(
        self, incident_key: str
    ) -> CollectorIncident | None: ...


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

    def set_incident_store(self, store: CollectorIncidentStore) -> None:
        """Set the store that persists collector-level incident state."""


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
        self._recovery_handler: Callable[[AddCheckResult], bool] | None = None
        self._process_notifier: Callable[[Check, Result], None] | None = None
        # Incident dedup/reminder state is persisted, not held in memory, so a
        # deploy or crash mid-incident cannot re-alert. The in-memory default
        # keeps a collector that was never handed a store from degrading to
        # one alert per iteration.
        self._incident_store: CollectorIncidentStore = InMemoryStore()
        self._incident_retry_keys: set[str] = set()
        self._startup_incidents_reconciled = False

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

    def set_incident_store(self, store: CollectorIncidentStore) -> None:
        """Persist collector-level incidents so a restart does not re-alert."""
        self._incident_store = store

    # ---------- collector-level incidents ----------
    async def _incident_get(self, incident_key: str) -> CollectorIncident | None:
        store = self._incident_store
        async_impl = getattr(store, "_get_collector_incident_async", None)
        if async_impl is not None:
            return await async_impl(incident_key)
        return store.get_collector_incident(incident_key)

    async def _incident_claim(
        self,
        incident_key: str,
        *,
        now: int,
        reminder_seconds: int,
        payload: dict[str, Any] | None = None,
    ) -> CollectorIncidentAlert:
        store = self._incident_store
        async_impl = getattr(store, "_claim_collector_incident_alert_async", None)
        if async_impl is not None:
            return await async_impl(incident_key, now, reminder_seconds, payload)
        return store.claim_collector_incident_alert(
            incident_key,
            now=now,
            reminder_seconds=reminder_seconds,
            payload=payload,
        )

    async def _incident_close(self, incident_key: str) -> CollectorIncident | None:
        self._incident_retry_keys.discard(incident_key)
        store = self._incident_store
        async_impl = getattr(store, "_close_collector_incident_async", None)
        if async_impl is not None:
            return await async_impl(incident_key)
        return store.close_collector_incident(incident_key)

    def _incident_reminder_seconds(self, incident_key: str, default: int) -> int:
        """Retry sooner when the previous alert for this incident failed to send."""
        if incident_key in self._incident_retry_keys:
            return ABANDONED_BATCH_RETRY_SECONDS
        return default

    DELIVERY_PENDING_KEY = "delivery_pending"

    async def _incident_set_payload(
        self, incident_key: str, payload: dict[str, Any]
    ) -> None:
        store = self._incident_store
        async_impl = getattr(store, "_set_collector_incident_payload_async", None)
        try:
            if async_impl is not None:
                await async_impl(incident_key, payload)
            else:
                setter = getattr(store, "set_collector_incident_payload", None)
                if setter is not None:
                    await to_thread.run_sync(
                        setter, incident_key, payload, abandon_on_cancel=False
                    )
        except Exception:
            logger.exception(
                "failed to persist delivery state for incident %s", incident_key
            )

    async def _record_incident_send(self, incident_key: str, sent: bool) -> None:
        """Record delivery outcome BOTH in memory and in the incident payload.

        The in-memory set alone is not durable. `_incident_claim` stamps
        `last_alert_at` *before* the notification is attempted, so after a
        restart following a failed send the incident looks recently alerted:
        no retry fires, and it resolves after the quiet period having paged
        nobody. Persisting the flag lets `_rehydrate_incident_retry_state()`
        restore the retry intent on the next iteration, in this process or a
        later one.
        """
        if sent:
            self._incident_retry_keys.discard(incident_key)
        else:
            self._incident_retry_keys.add(incident_key)
        existing = await self._incident_get(incident_key)
        if existing is None:
            return
        payload = dict(existing.payload)
        if sent:
            payload.pop(self.DELIVERY_PENDING_KEY, None)
        else:
            payload[self.DELIVERY_PENDING_KEY] = True
        if payload != existing.payload:
            await self._incident_set_payload(incident_key, payload)

    def _rehydrate_incident_retry_state(
        self, incident_key: str, existing: CollectorIncident | None
    ) -> None:
        """Restore the retry marker from persisted state after a restart."""
        if existing is None:
            return
        if existing.payload.get(self.DELIVERY_PENDING_KEY):
            self._incident_retry_keys.add(incident_key)

    async def _notify_collector_incident(
        self,
        *,
        incident_key: str,
        name: str,
        error_type: str,
        error_msg: str,
        alert_count: int,
    ) -> bool:
        """Send exactly one collector-scoped notification for an incident.

        The synthetic check row exists only to reuse the notifier's message and
        ticket shape; it is never persisted. ``incident_key`` gives the alert a
        stable identity so downstream ticketing deduplicates it too.
        """
        if self._process_notifier is None:
            logger.error("collector-wide notifier is not configured")
            return False
        check = Check(
            check_id=0,
            service_id=0,
            name=name,
            check_type="internal",
            url="internal://collector",
            data={},
        )
        result = Result(
            check_id=0,
            status=ResultStatus.ERROR,
            data={
                "error_type": error_type,
                "error_msg": error_msg,
                "incident_key": incident_key,
                "incident_alert_count": alert_count,
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
            logger.exception(
                "failed to send the collector incident notification for %s",
                incident_key,
            )
            return False

    async def _reconcile_incidents_after_restart(self) -> None:
        """Close incidents that cannot outlive the process that opened them."""
        try:
            closed = await self._incident_close(COLLECTOR_PAUSED_INCIDENT_KEY)
        except Exception:
            logger.exception("failed to reconcile collector incidents on startup")
            return
        if closed is not None:
            logger.warning(
                "collector execution-paused incident opened at %s was resolved on "
                "startup after %s alert(s); the abandoned batch thread did not "
                "survive the restart",
                closed.opened_at,
                closed.alert_count,
            )

    @staticmethod
    def _merge_stale_batch_payload(
        existing: CollectorIncident | None, recovered: list[Check], now: int
    ) -> dict[str, Any]:
        previous = dict(existing.payload) if existing is not None else {}
        try:
            reclaimed = int(previous.get("reclaimed_count", 0))
            iterations = int(previous.get("iteration_count", 0))
        except (TypeError, ValueError):
            reclaimed, iterations = 0, 0
        sample = [
            check_id
            for check_id in previous.get("check_ids", [])
            if isinstance(check_id, int) and not isinstance(check_id, bool)
        ]
        for check in recovered:
            if (
                check.check_id not in sample
                and len(sample) < INCIDENT_CHECK_SAMPLE_LIMIT
            ):
                sample.append(check.check_id)
        return {
            "incident_type": "stale_processing_lease",
            "reclaimed_count": reclaimed + len(recovered),
            "iteration_count": iterations + 1,
            "check_ids": sample,
            "last_seen_at": now,
        }

    @staticmethod
    def _stale_batch_message(incident: CollectorIncident) -> str:
        payload = incident.payload
        count = payload.get("reclaimed_count", 0)
        iterations = payload.get("iteration_count", 0)
        sample = list(payload.get("check_ids", []))[:INCIDENT_MESSAGE_ID_LIMIT]
        try:
            window = max(
                0,
                int(payload.get("last_seen_at", incident.opened_at))
                - incident.opened_at,
            )
        except (TypeError, ValueError):
            window = 0
        listed = ", ".join(str(check_id) for check_id in sample) or "none recorded"
        if isinstance(count, int) and count > len(sample):
            listed += f" (+{count - len(sample)} more)"
        return (
            f"{count} check processing lease(s) expired and were reclaimed over "
            f"{window}s across {iterations} collector iteration(s). "
            f"Affected check ids: {listed}. "
            "Every affected check was released and rescheduled. This is one "
            "batch-level incident: lease recovery never pages per check."
        )

    async def _sync_stale_batch_incident(self, recovered: list[Check]) -> None:
        """Fold a burst of expired leases into one deduplicated incident."""
        incident_key = COLLECTOR_STALE_BATCH_INCIDENT_KEY
        now = int(current_epoch())
        try:
            if recovered:
                existing = await self._incident_get(incident_key)
                self._rehydrate_incident_retry_state(incident_key, existing)
                alert = await self._incident_claim(
                    incident_key,
                    now=now,
                    reminder_seconds=self._incident_reminder_seconds(
                        incident_key, STALE_BATCH_REMINDER_SECONDS
                    ),
                    payload=self._merge_stale_batch_payload(existing, recovered, now),
                )
                logger.warning(
                    "reclaimed %s expired check processing lease(s); tracked as a "
                    "single collector incident (%s alert(s) so far)",
                    len(recovered),
                    alert.incident.alert_count,
                )
                if alert.should_notify:
                    sent = await self._notify_collector_incident(
                        incident_key=incident_key,
                        name="Nyxmon collector (stale check leases)",
                        error_type="stale_processing_lease_batch",
                        error_msg=self._stale_batch_message(alert.incident),
                        alert_count=alert.incident.alert_count,
                    )
                    await self._record_incident_send(incident_key, sent)
                return

            existing = await self._incident_get(incident_key)
            if existing is None:
                return
            self._rehydrate_incident_retry_state(incident_key, existing)

            # Retry a delivery that failed earlier. Without this, a one-off
            # stale batch whose first notification failed was never re-sent:
            # _record_incident_send() marked it for retry, but only the
            # `recovered` branch above ever claims an alert, and a one-off batch
            # produces no further reclaims. The incident then resolved silently
            # after STALE_BATCH_RESOLVE_AFTER_SECONDS having paged nobody.
            if incident_key in self._incident_retry_keys:
                alert = await self._incident_claim(
                    incident_key,
                    now=now,
                    reminder_seconds=self._incident_reminder_seconds(
                        incident_key, STALE_BATCH_REMINDER_SECONDS
                    ),
                )
                if alert.should_notify:
                    sent = await self._notify_collector_incident(
                        incident_key=incident_key,
                        name="Nyxmon collector (stale check leases)",
                        error_type="stale_processing_lease_batch",
                        error_msg=self._stale_batch_message(alert.incident),
                        alert_count=alert.incident.alert_count,
                    )
                    await self._record_incident_send(incident_key, sent)
                    if not sent:
                        # Keep the incident open while delivery is still failing
                        # rather than resolving an alert nobody ever received.
                        return

            try:
                last_seen = int(
                    existing.payload.get("last_seen_at") or existing.opened_at
                )
            except (TypeError, ValueError):
                last_seen = existing.opened_at
            if now - last_seen < STALE_BATCH_RESOLVE_AFTER_SECONDS:
                return
            if incident_key in self._incident_retry_keys:
                # Never close an incident whose alert was never delivered.
                return
            await self._incident_close(incident_key)
            logger.info(
                "stale processing-lease incident resolved after %s reclaimed lease(s) "
                "and %s alert(s)",
                existing.payload.get("reclaimed_count", 0),
                existing.alert_count,
            )
        except Exception:
            logger.exception("failed to update the stale processing-lease incident")

    def _paused_batch_message(self, incident: CollectorIncident) -> str:
        payload = incident.payload
        size = payload.get("batch_size", 0)
        sample = list(payload.get("check_ids", []))[:INCIDENT_MESSAGE_ID_LIMIT]
        elapsed = max(0, int(current_epoch()) - incident.opened_at)
        listed = ", ".join(str(check_id) for check_id in sample)
        message = (
            "All new check executions are paused because an abandoned batch "
            f"thread is still running after {elapsed}s. The wedged batch holds "
            f"{size} check(s)"
        )
        if listed:
            message += f" (check ids: {listed})"
        return (
            message + ". Expired processing leases are still being reclaimed and "
            "rescheduled while execution stays paused."
        )

    async def _sync_paused_batch_incident(self) -> None:
        """Alert once for a wedged executor, then remind on elapsed time."""
        incident_key = COLLECTOR_PAUSED_INCIDENT_KEY
        try:
            alert = await self._incident_claim(
                incident_key,
                now=int(current_epoch()),
                reminder_seconds=self._incident_reminder_seconds(
                    incident_key, ABANDONED_BATCH_REMINDER_SECONDS
                ),
                payload={
                    "incident_type": "collector_execution_paused",
                    "batch_size": len(self._abandoned_batch_checks),
                    "check_ids": [
                        check.check_id
                        for check in self._abandoned_batch_checks[
                            :INCIDENT_CHECK_SAMPLE_LIMIT
                        ]
                    ],
                },
            )
            if not alert.should_notify:
                return
            logger.error(
                "abandoned check batch is still running; new executions are paused"
            )
            sent = await self._notify_collector_incident(
                incident_key=incident_key,
                name="Nyxmon collector (execution paused)",
                error_type="collector_execution_paused",
                error_msg=self._paused_batch_message(alert.incident),
                alert_count=alert.incident.alert_count,
            )
            await self._record_incident_send(incident_key, sent)
        except Exception:
            logger.exception("failed to update the collector execution-paused incident")

    async def _resolve_paused_batch_incident(self) -> None:
        try:
            closed = await self._incident_close(COLLECTOR_PAUSED_INCIDENT_KEY)
        except Exception:
            logger.exception(
                "failed to resolve the collector execution-paused incident"
            )
            return
        if closed is not None:
            logger.info(
                "abandoned check batch finished; execution resumed and the "
                "collector incident was resolved after %s alert(s)",
                closed.alert_count,
            )

    async def _record_stale_check(self, check: Check) -> bool:
        """Record and reschedule one expired lease without alerting per check.

        The result is still persisted so the check's history shows why it has a
        gap, but it carries ``collector_internal`` so it can never become a
        per-check external notification: the endpoint was never observed, and
        reclaiming a batch of N checks must not page N times. The batch is
        reported once, at collector level, by
        :meth:`_sync_stale_batch_incident`.
        """
        try:
            stale_result = Result(
                check_id=check.check_id,
                status=ResultStatus.ERROR,
                data={
                    "error_type": "stale_processing_lease",
                    "error_msg": (
                        "Check processing lease expired; the previous worker "
                        "did not persist a result"
                    ),
                    "collector_internal": True,
                },
            )
            check.schedule_next_check()
            # A production bootstrap supplies an independently injected handler
            # with its own UnitOfWork. It must not share transaction state with
            # an abandoned ExecuteChecks handler that may still be running.
            handler = self._recovery_handler
            if handler is None:
                raise RuntimeError("lease recovery handler is not configured")
            await to_thread.run_sync(
                handler,
                AddCheckResult(
                    check_result=CheckResult(check=check, result=stale_result)
                ),
                abandon_on_cancel=False,
            )
            return True
        except Exception:
            logger.exception(
                "failed to record expired processing lease for check_id=%s",
                check.check_id,
            )
            return False

    async def _collect_once(self) -> None:
        if not self._startup_incidents_reconciled:
            self._startup_incidents_reconciled = True
            await self._reconcile_incidents_after_restart()
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
        stale_checks: list[Check] = []
        for round_number in range(MAX_STALE_RECLAIM_ROUNDS):
            try:
                batch = await self._bus.uow.store.checks.reclaim_stale_checks_async(
                    effective_lease
                )
            except Exception:
                logger.exception("processing-lease reclaim failed")
                break
            if not batch:
                break
            stale_checks.extend(batch)
            if round_number == MAX_STALE_RECLAIM_ROUNDS - 1:
                logger.warning(
                    "reclaimed %s expired leases without draining the backlog; "
                    "the remainder is handled by the next collector iteration",
                    len(stale_checks),
                )

        recovered: list[Check] = []
        for check in stale_checks:
            if not check.disabled and await self._record_stale_check(check):
                recovered.append(check)
        await self._sync_stale_batch_incident(recovered)

        if self._abandoned_batch_done is not None:
            if not self._abandoned_batch_done.is_set():
                await self._sync_paused_batch_incident()
                return
            self._abandoned_batch_done = None
            self._abandoned_batch_checks = []
            await self._resolve_paused_batch_incident()

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
