from __future__ import annotations

from dataclasses import dataclass, field
import copy
import threading

from typing import Any, List
import time

from ...domain import Check, CheckStatus, Result, Service
from .interface import (
    DELIVERY_ATTEMPT_KEY,
    CollectorIncident,
    CollectorIncidentAlert,
    HeldCheck,
    NotificationState,
    NotificationTransition,
    Repository,
    ResultRepository,
    CheckRepository,
    ServiceRepository,
    RepositoryStore,
    NotificationStateConflict,
    carry_delivery_markers,
    check_batch_size,
    with_delivery_intent,
    without_delivery_markers,
)


class InMemoryResultRepository(ResultRepository):
    """An in-memory implementation of the ResultRepository interface."""

    def __init__(self) -> None:
        self.results: dict[int, Result] = {}
        self.seen: set[Result] = set()
        self._timestamps: dict[int, int] = {}  # result_id -> timestamp

    def add(self, result: Result) -> None:
        if result.result_id is None:
            result.result_id = len(self.results)
        self.results[result.result_id] = result
        self.seen.add(result)
        # Store current timestamp
        import time

        self._timestamps[result.result_id] = int(time.time())

    def get(self, result_id: int) -> Result:
        return self.results[result_id]

    def list(self) -> List[Result]:
        return list(self.results.values())

    def list_for_check(self, check_id: int, limit: int) -> List[Result]:
        results = [
            result for result in self.results.values() if result.check_id == check_id
        ]

        def result_id(result: Result) -> int:
            assert result.result_id is not None
            return result.result_id

        return sorted(
            results,
            key=result_id,
            reverse=True,
        )[:limit]

    async def delete_old_results_async(
        self, retention_seconds: int = 86400, batch_size: int = 1000
    ) -> int:
        """Delete check results older than the specified period."""
        import time

        current_time = int(time.time())
        cutoff_time = current_time - retention_seconds

        # Find old results
        old_result_ids = [
            result_id
            for result_id, timestamp in self._timestamps.items()
            if timestamp < cutoff_time
        ]

        # Limit by batch size
        to_delete = old_result_ids[:batch_size]

        # Delete the results
        deleted_count = 0
        for result_id in to_delete:
            if result_id in self.results:
                del self.results[result_id]
                del self._timestamps[result_id]
                deleted_count += 1

        return deleted_count

    def delete_old_results(
        self, retention_seconds: int = 86400, batch_size: int = 1000
    ) -> int:
        """Delete check results older than the specified period."""
        import asyncio

        loop = asyncio.get_event_loop()
        return loop.run_until_complete(
            self.delete_old_results_async(
                retention_seconds=retention_seconds, batch_size=batch_size
            )
        )


class InMemoryCheckRepository(CheckRepository):
    """An in-memory implementation of the CheckRepository interface.

    The scheduling and notification dictionaries are read by the site
    connectivity observer from its own thread while the collector's worker
    threads write them, so every method that touches them takes ``lock``.
    :class:`InMemoryStore` replaces this lock with a store-wide one, which is
    what makes a completion (claim release plus notification state plus hold
    marker) one critical section instead of a sequence an observer can read
    between.
    """

    def __init__(self) -> None:
        self.checks: dict[int, Check] = {}
        self.notification_states: dict[int, NotificationState] = {}
        self.seen: set[Check] = set()
        #: Reentrant so a store-level critical section can call these methods.
        self.lock: threading.RLock = threading.RLock()

    def add(self, check: Check) -> None:
        self.checks[check.check_id] = check
        self.seen.add(check)

    def get(self, check_id: int) -> Check:
        return self.checks[check_id]

    def list(self) -> List[Check]:
        return list(self.checks.values())

    async def list_async(self) -> List[Check]:
        """Return checks in an awaitable form for async callers."""
        return self.list()

    async def list_due_checks_async(self) -> List[Check]:
        with self.lock:
            current_time = int(time.time())
            claimed: list[Check] = []
            candidates = sorted(
                self.checks.values(),
                key=lambda check: (check.next_check_time, check.check_id),
            )
            for check in candidates:
                if (
                    check.next_check_time <= current_time
                    and check.status == CheckStatus.IDLE
                    and not check.disabled
                ):
                    check.status = CheckStatus.PROCESSING
                    check.processing_started_at = current_time
                    check.claim_started_at = current_time
                    claimed.append(copy.deepcopy(check))
                    if len(claimed) >= check_batch_size():
                        break
            return claimed

    async def reclaim_stale_checks_async(self, lease_seconds: int) -> List[Check]:
        with self.lock:
            current_time = int(time.time())
            stale_before = current_time - lease_seconds
            reclaimed: list[Check] = []
            for check in self.checks.values():
                if (
                    check.status == CheckStatus.PROCESSING
                    and not check.processing_started_at
                ):
                    check.processing_started_at = current_time
            candidates = sorted(
                (
                    check
                    for check in self.checks.values()
                    if (
                        check.status == CheckStatus.PROCESSING
                        and check.processing_started_at > 0
                        and check.processing_started_at <= stale_before
                    )
                ),
                key=lambda check: (check.processing_started_at, check.check_id),
            )
            for check in candidates:
                if (
                    check.status == CheckStatus.PROCESSING
                    and check.processing_started_at <= stale_before
                ):
                    check.status = CheckStatus.IDLE
                    check.processing_started_at = 0
                    check.claim_started_at = 0
                    reclaimed.append(copy.deepcopy(check))
                    if len(reclaimed) >= check_batch_size():
                        break
            return reclaimed

    def get_notification_state(self, check_id: int) -> NotificationState:
        with self.lock:
            return self.notification_states.get(check_id, NotificationState())

    def set_notification_state(self, check_id: int, state: NotificationState) -> None:
        with self.lock:
            self.notification_states[check_id] = state

    def acknowledge_notification_attempt(self, check_id: int, attempt_seq: int) -> bool:
        """Clear the pending delivery marker of one notification attempt."""
        with self.lock:
            state = self.notification_states.get(check_id)
            if (
                state is None
                or state.attempt_at == 0
                or state.attempt_seq != attempt_seq
            ):
                return False
            self.notification_states[check_id] = state.evolve(attempt_at=0)
            return True

    def _held_check_ids(self) -> List[int]:
        with self.lock:
            return sorted(
                check_id
                for check_id, state in self.notification_states.items()
                if state.held_since > 0 and check_id in self.checks
            )

    async def list_held_checks_async(self) -> List[HeldCheck]:
        """Return every check whose notification state has ``held_since > 0``."""
        with self.lock:
            held: List[HeldCheck] = []
            for check_id in self._held_check_ids():
                check = self.checks[check_id]
                held.append(
                    HeldCheck(
                        check_id=check_id,
                        data=copy.deepcopy(check.data),
                        status=str(check.status),
                        disabled=bool(check.disabled),
                        next_check_time=check.next_check_time,
                        held_since=self.notification_states[check_id].held_since,
                    )
                )
            return held

    def count_held_checks(self) -> int:
        """Return the number of checks with ``held_since > 0``."""
        with self.lock:
            return len(self._held_check_ids())

    async def count_held_checks_async(self) -> int:
        """Return the number of checks with ``held_since > 0``."""
        return self.count_held_checks()

    async def count_processing_claims_before_async(self, epoch: int) -> int:
        """Count executions claimed before ``epoch`` that are still in flight."""
        with self.lock:
            return sum(
                1
                for check in self.checks.values()
                if check.status == CheckStatus.PROCESSING
                and check.processing_started_at > 0
                and check.processing_started_at < epoch
            )

    async def reschedule_checks_async(
        self, check_ids: List[int], *, run_at: int
    ) -> int:
        """Pull idle, enabled checks forward to ``run_at``."""
        if not check_ids:
            return 0
        with self.lock:
            changed = 0
            for check_id in check_ids:
                check = self.checks.get(check_id)
                if check is None:
                    continue
                if (
                    check.status == CheckStatus.IDLE
                    and not check.disabled
                    and check.next_check_time > run_at
                ):
                    check.next_check_time = run_at
                    changed += 1
            return changed


class InMemoryServiceRepository(ServiceRepository):
    """An in-memory implementation of the ServiceRepository interface."""

    def __init__(self) -> None:
        self.services: dict[int, Service] = {}
        self.seen: set[Service] = set()

    def add(self, service: Service) -> None:
        self.services[service.service_id] = service
        self.seen.add(service)

    def get(self, service_id: int) -> Service:
        return self.services[service_id]

    def list(self) -> List[Service]:
        return list(self.services.values())


@dataclass(slots=True)
class InMemoryStore(RepositoryStore):
    """An in-memory store for the repositories.

    ``lock`` serialises every access to the mutable backing data. The site
    connectivity observer runs in its own thread and reads the processing
    claims and the held set to decide what to recheck; without one mutex over
    the whole completion it could see a claim already released and the hold
    marker not yet written, i.e. neither "in flight" nor "held", and drop the
    recheck obligation. SQLite gets the same guarantee from its single
    ``BEGIN IMMEDIATE`` transaction.
    """

    results: InMemoryResultRepository = field(default_factory=InMemoryResultRepository)
    checks: InMemoryCheckRepository = field(default_factory=InMemoryCheckRepository)
    services: InMemoryServiceRepository = field(
        default_factory=InMemoryServiceRepository
    )
    collector_incidents: dict[str, CollectorIncident] = field(default_factory=dict)
    #: Store-wide, reentrant: a critical section here calls repository methods
    #: that take the same lock again.
    lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        # One mutex for the store and its check repository, so a completion and
        # the observer's reads can never interleave.
        self.checks.lock = self.lock

    def fork_for_concurrent_uow(self) -> InMemoryStore:
        """Share backing data, and the store lock, while isolating event sets."""
        results = InMemoryResultRepository()
        results.results = self.results.results
        results._timestamps = self.results._timestamps
        checks = InMemoryCheckRepository()
        checks.checks = self.checks.checks
        checks.notification_states = self.checks.notification_states
        services = InMemoryServiceRepository()
        services.services = self.services.services
        return InMemoryStore(
            results=results,
            checks=checks,
            services=services,
            collector_incidents=self.collector_incidents,
            lock=self.lock,
        )

    def persist_check_result(
        self,
        check: Check,
        result: Result,
        notification_transition: NotificationTransition | None,
        *,
        complete_check: bool = True,
        hold_marker: int | None = None,
    ) -> bool:
        # One critical section: the claim release, the result, the notification
        # state and the hold marker become visible together or not at all.
        with self.lock:
            current_check = self.checks.checks.get(check.check_id)
            if current_check is None:
                return False
            completion_superseded = complete_check and (
                (
                    current_check.status == CheckStatus.PROCESSING
                    and current_check.processing_started_at != check.claim_started_at
                )
                or (
                    current_check.status != CheckStatus.PROCESSING
                    and bool(check.claim_started_at)
                )
            )
            if completion_superseded:
                self.results.add(result)
                return False
            if notification_transition is not None:
                expected_state, notification_state = notification_transition
                if self.checks.get_notification_state(check.check_id) != expected_state:
                    raise NotificationStateConflict(check.check_id)
            self.results.add(result)
            if complete_check:
                for attribute in ("status", "next_check_time", "processing_started_at"):
                    setattr(current_check, attribute, getattr(check, attribute))
                current_check.claim_started_at = 0
                self.checks.seen.add(current_check)
            if notification_transition is not None:
                expected_state, notification_state = notification_transition
                if notification_state != expected_state:
                    self.checks.set_notification_state(
                        check.check_id, notification_state
                    )
            elif hold_marker is not None:
                # Conflict-exhaustion fallback: the hold lands with the
                # completion.
                held_state = self.checks.get_notification_state(check.check_id)
                if held_state.held_since == 0:
                    self.checks.set_notification_state(
                        check.check_id, held_state.evolve(held_since=hold_marker)
                    )
            return True

    # ---------- collector-level incidents ----------
    def get_collector_incident(self, incident_key: str) -> CollectorIncident | None:
        with self.lock:
            return self.collector_incidents.get(incident_key)

    def claim_collector_incident_alert(
        self,
        incident_key: str,
        *,
        now: int,
        reminder_seconds: int,
        payload: dict[str, Any] | None = None,
    ) -> CollectorIncidentAlert:
        with self.lock:
            existing = self.collector_incidents.get(incident_key)
            if existing is None:
                incident = CollectorIncident(
                    incident_key=incident_key,
                    opened_at=now,
                    last_alert_at=now,
                    alert_count=1,
                    payload=with_delivery_intent(dict(payload or {}), 1),
                )
                self.collector_incidents[incident_key] = incident
                return CollectorIncidentAlert(
                    incident=incident, should_notify=True, is_new=True
                )
            should_notify = now - existing.last_alert_at >= max(1, reminder_seconds)
            alert_count = existing.alert_count + (1 if should_notify else 0)
            next_payload = (
                dict(payload) if payload is not None else dict(existing.payload)
            )
            incident = CollectorIncident(
                incident_key=incident_key,
                opened_at=existing.opened_at,
                last_alert_at=now if should_notify else existing.last_alert_at,
                alert_count=alert_count,
                payload=(
                    with_delivery_intent(next_payload, alert_count)
                    if should_notify
                    else carry_delivery_markers(next_payload, existing.payload)
                ),
            )
            self.collector_incidents[incident_key] = incident
            return CollectorIncidentAlert(
                incident=incident, should_notify=should_notify, is_new=False
            )

    def open_collector_incident(
        self, incident_key: str, *, now: int, payload: dict[str, Any]
    ) -> CollectorIncident:
        """Create an incident without claiming an alert, or replace its payload."""
        with self.lock:
            existing = self.collector_incidents.get(incident_key)
            if existing is None:
                incident = CollectorIncident(
                    incident_key=incident_key,
                    opened_at=now,
                    last_alert_at=0,
                    alert_count=0,
                    payload=dict(payload),
                )
            else:
                incident = CollectorIncident(
                    incident_key=existing.incident_key,
                    opened_at=existing.opened_at,
                    last_alert_at=existing.last_alert_at,
                    alert_count=existing.alert_count,
                    payload=carry_delivery_markers(dict(payload), existing.payload),
                )
            self.collector_incidents[incident_key] = incident
            return incident

    def close_collector_incident(self, incident_key: str) -> CollectorIncident | None:
        with self.lock:
            return self.collector_incidents.pop(incident_key, None)

    def acknowledge_collector_incident_delivery(
        self, incident_key: str, attempt: int
    ) -> bool:
        """Clear the delivery markers of ``attempt`` under the store lock.

        The compare and the write share one critical section, so a claim
        granted concurrently either runs first (and this acknowledgement no
        longer matches the stored attempt) or afterwards (and rewrites its own,
        newer intent). Keys other than the delivery markers are read from the
        row here, never supplied by the caller, so a payload refreshed since
        the claim is not rolled back.
        """
        with self.lock:
            existing = self.collector_incidents.get(incident_key)
            if (
                existing is None
                or existing.payload.get(DELIVERY_ATTEMPT_KEY) != attempt
            ):
                return False
            self.collector_incidents[incident_key] = CollectorIncident(
                incident_key=existing.incident_key,
                opened_at=existing.opened_at,
                last_alert_at=existing.last_alert_at,
                alert_count=existing.alert_count,
                payload=without_delivery_markers(existing.payload),
            )
            return True

    def set_collector_incident_payload(
        self, incident_key: str, payload: dict[str, Any]
    ) -> CollectorIncident | None:
        with self.lock:
            existing = self.collector_incidents.get(incident_key)
            if existing is None:
                return None
            updated = CollectorIncident(
                incident_key=existing.incident_key,
                opened_at=existing.opened_at,
                last_alert_at=existing.last_alert_at,
                alert_count=existing.alert_count,
                payload=dict(payload),
            )
            self.collector_incidents[incident_key] = updated
            return updated

    def list(self) -> List[Repository]:
        return [
            self.results,
            self.checks,
            self.services,
        ]
