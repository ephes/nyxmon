import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any, List, Protocol, TypeAlias

from ...domain import Result, Check, Service

DEFAULT_CHECK_BATCH_SIZE = 5
MAX_CHECK_BATCH_SIZE = 100
logger = logging.getLogger(__name__)


@lru_cache(maxsize=None)
def _check_batch_size_from_value(value: str) -> int:
    if not value:
        return DEFAULT_CHECK_BATCH_SIZE
    try:
        parsed = int(value)
    except ValueError:
        logger.warning(
            "NYXMON_CHECK_BATCH_SIZE is invalid; using default %s",
            DEFAULT_CHECK_BATCH_SIZE,
        )
        return DEFAULT_CHECK_BATCH_SIZE
    return max(1, min(parsed, MAX_CHECK_BATCH_SIZE))


def check_batch_size() -> int:
    value = os.environ.get("NYXMON_CHECK_BATCH_SIZE", "").strip()
    return _check_batch_size_from_value(value)


class NotificationStateConflict(RuntimeError):
    """The notification state changed after a caller calculated its update."""


@dataclass(frozen=True, slots=True)
class NotificationState:
    """Per-check alert cadence state.

    Persisted in ``check_notification_state``. Compared as a whole for
    optimistic concurrency control, so every field participates in the
    compare-and-swap performed by :meth:`RepositoryStore.persist_check_result`.

    Attributes:
        failure_count: Consecutive non-OK samples in the current incident.
        last_attempt_count: ``failure_count`` at the last external notification.
            Bookkeeping/diagnostics only - it no longer drives reminder cadence.
        last_immediate_at: Epoch of the last ``notification_immediate`` alert.
        last_notified_at: Epoch of the last external notification for the
            current incident. ``0`` means "this incident has never alerted".
        first_failure_at: Epoch of the first sample in the current incident.
        held_since: Epoch of the first sample held back because a site
            dependency of this check is down. ``0`` when the check is not
            currently held. It bounds the hold (``NYXMON_SITE_MAX_HOLD_SECONDS``)
            and is the observer's list of checks that owe a recheck, so it is
            reset only by an OK sample or by an observed dependency recovery -
            never by a bypass caused by a stale snapshot or an exhausted
            budget, and never by a maintenance-suppressed sample.
        attempt_seq: Monotonic counter of external notification attempts for
            this check. Incremented in the same compare-and-swap that decides
            to alert, before any I/O, and used to fence the acknowledgement
            written after the send. It is never reset, so an acknowledgement
            that arrives after an intervening OK or a newer attempt cannot
            clear the wrong intent.
        attempt_at: Epoch of the current unacknowledged notification attempt,
            ``0`` when none is pending. A value greater than zero after a
            restart is a durable "this alert may never have been delivered"
            marker that drives the delivery retry. Only an acknowledgement or
            an OK sample clears it; a maintenance-suppressed sample does not
            cancel a delivery that was already claimed.
    """

    failure_count: int = 0
    last_attempt_count: int = 0
    last_immediate_at: int = 0
    last_notified_at: int = 0
    first_failure_at: int = 0
    held_since: int = 0
    attempt_seq: int = 0
    attempt_at: int = 0

    @classmethod
    def from_row(cls, row: "Sequence[Any] | None") -> "NotificationState":
        """Build a state from a database row, tolerating pre-upgrade rows."""
        if row is None:
            return cls()
        field_count = len(NOTIFICATION_STATE_COLUMNS)
        values = [int(value or 0) for value in row[:field_count]]
        values.extend([0] * (field_count - len(values)))
        return cls(*values)

    def as_row(self) -> tuple[int, int, int, int, int, int, int, int]:
        return (
            self.failure_count,
            self.last_attempt_count,
            self.last_immediate_at,
            self.last_notified_at,
            self.first_failure_at,
            self.held_since,
            self.attempt_seq,
            self.attempt_at,
        )

    def cleared(self) -> "NotificationState":
        """Full reset used when a check recovers.

        ``attempt_seq`` is carried over: it is a monotonic fence, not incident
        state, and reusing a sequence number would let a late acknowledgement
        clear a newer attempt.
        """
        return NotificationState(attempt_seq=self.attempt_seq)

    def with_streak_reset(self) -> "NotificationState":
        """Reset the failure streak of a maintenance-suppressed sample.

        Only the incident bookkeeping (``failure_count``, ``first_failure_at``
        and the reminder timestamps) is cleared. A maintenance window is
        evidence about this check's alerting, not about its site dependency and
        not about a send nobody has acknowledged, so ``held_since`` and
        ``attempt_at`` are carried over unchanged:

        * zeroing ``held_since`` would re-arm the bounded hold budget for the
          rest of an ongoing outage and drop the check from the observer's
          recheck set;
        * zeroing ``attempt_at`` would cancel a pending delivery intent that
          was never acknowledged.

        An OK sample clears both, through :meth:`cleared`. ``attempt_seq`` is
        carried over for the same reason as there: it is a monotonic fence,
        not incident state.
        """
        return NotificationState(
            last_immediate_at=self.last_immediate_at,
            held_since=self.held_since,
            attempt_seq=self.attempt_seq,
            attempt_at=self.attempt_at,
        )

    def evolve(self, **changes: int) -> "NotificationState":
        return replace(self, **changes)


NOTIFICATION_STATE_COLUMNS = (
    "failure_count",
    "last_attempt_count",
    "last_immediate_at",
    "last_notified_at",
    "first_failure_at",
    "held_since",
    "attempt_seq",
    "attempt_at",
)

NotificationTransition: TypeAlias = tuple[NotificationState, NotificationState]

DELIVERY_PENDING_KEY = "delivery_pending"
DELIVERY_ATTEMPT_KEY = "delivery_attempt"
DELIVERY_MARKER_KEYS = (DELIVERY_PENDING_KEY, DELIVERY_ATTEMPT_KEY)


def carry_delivery_markers(
    payload: dict[str, Any], existing: dict[str, Any]
) -> dict[str, Any]:
    """Copy the repository-owned delivery markers of ``existing`` into ``payload``.

    ``delivery_pending`` and ``delivery_attempt`` record that an alert was
    claimed but not yet acknowledged. They belong to the repository, so a
    caller that rebuilds an incident payload from scratch (a second reclaimed
    lease refreshing the stale-batch details, for example) must not silently
    erase a pending retry intent.

    Args:
        payload: The payload that is about to be written.
        existing: The payload currently stored for the incident.

    Returns:
        A copy of ``payload`` with the markers of ``existing`` restored.
    """
    merged = dict(payload)
    for key in DELIVERY_MARKER_KEYS:
        if key in existing:
            merged[key] = existing[key]
    return merged


def with_delivery_intent(payload: dict[str, Any], attempt: int) -> dict[str, Any]:
    """Return ``payload`` with a durable "alert claimed, not yet sent" marker.

    Args:
        payload: The payload that is about to be written.
        attempt: The alert count this claim granted.

    Returns:
        A copy of ``payload`` carrying the delivery markers of ``attempt``.
    """
    merged = dict(payload)
    merged[DELIVERY_PENDING_KEY] = True
    merged[DELIVERY_ATTEMPT_KEY] = attempt
    return merged


def without_delivery_markers(payload: dict[str, Any]) -> dict[str, Any]:
    """Return ``payload`` with the repository-owned delivery markers removed.

    The inverse of :func:`with_delivery_intent`, used by the acknowledgement of
    a delivered alert. Every other key of the stored payload is preserved, so
    acknowledging a send can never roll back incident details written since the
    claim.

    Args:
        payload: The payload currently stored for the incident.

    Returns:
        A copy of ``payload`` without ``delivery_pending``/``delivery_attempt``.
    """
    merged = dict(payload)
    for key in DELIVERY_MARKER_KEYS:
        merged.pop(key, None)
    return merged


@dataclass(frozen=True, slots=True)
class HeldCheck:
    """A check whose alerting is currently held by a site dependency.

    Joined view of ``check_notification_state.held_since > 0`` and its
    ``health_check`` row: enough for the observer to decide whether the check
    still owes a recheck and whether it can be pulled forward.

    Attributes:
        check_id: Primary key of the held check.
        data: The check's ``data`` column, which carries ``site_dependency``.
        status: Current scheduling status (``idle``/``processing``).
        disabled: Whether the check is currently disabled.
        next_check_time: Epoch of the check's next scheduled execution.
        held_since: Epoch of the first held sample of the current hold.
    """

    check_id: int
    data: dict[str, Any]
    status: str
    disabled: bool
    next_check_time: int
    held_since: int


@dataclass(frozen=True, slots=True)
class CollectorIncident:
    """A persisted, deduplicated collector/batch-level incident.

    One row per ``incident_key``. Survives process restarts, which is what
    turns a wedged-batch or stale-lease event into a single bounded incident
    with timed reminders instead of one alert per affected check.
    """

    incident_key: str
    opened_at: int
    last_alert_at: int
    alert_count: int
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CollectorIncidentAlert:
    """Outcome of :meth:`RepositoryStore.claim_collector_incident_alert`."""

    incident: CollectorIncident
    should_notify: bool
    is_new: bool


class ResultRepository(Protocol):
    """A repository interface for storing and retrieving results."""

    seen: set[Result]

    def add(self, result: Result) -> None:
        """Add a result to the repository."""
        ...

    def get(self, result_id: int) -> Result:
        """Get a result from the repository by ID."""
        ...

    def list(self) -> List[Result]:
        """Get a list of all results."""
        ...

    def list_for_check(self, check_id: int, limit: int) -> List[Result]:
        """Get recent results for a check, newest first."""
        ...


class CheckRepository(Protocol):
    """A repository interface for storing and retrieving checks."""

    seen: set

    def add(self, check) -> None:
        """Add a check to the repository."""
        ...

    def get(self, check_id: int):
        """Get a check from the repository by ID."""
        ...

    def list(self) -> List[Check]:
        """Get a list of all checks."""
        ...

    async def list_async(self) -> List[Check]:
        """Get a list of all checks asynchronously."""
        ...

    async def list_due_checks_async(self) -> List[Check]:
        """Atomically claim checks due for execution."""
        ...

    async def reclaim_stale_checks_async(self, lease_seconds: int) -> List[Check]:
        """Release and return checks whose processing lease expired."""
        ...

    def get_notification_state(self, check_id: int) -> NotificationState:
        """Return the persisted alert cadence state for a check."""
        ...

    def set_notification_state(self, check_id: int, state: NotificationState) -> None:
        """Persist notification state independently from editable check data."""
        ...

    async def list_held_checks_async(self) -> List[HeldCheck]:
        """Return every check whose notification state has ``held_since > 0``.

        Returns:
            The joined check rows, so the caller can evaluate the dependency
            and the schedule without a second query per check.
        """
        ...

    async def reschedule_checks_async(
        self, check_ids: List[int], *, run_at: int
    ) -> int:
        """Pull idle, enabled checks forward to ``run_at``.

        Only rows that are still idle, still enabled, and not already due
        earlier are touched, so a recheck can never delay a check or revive a
        disabled one.

        Args:
            check_ids: Checks to pull forward. An empty list is a no-op.
            run_at: Epoch to schedule the checks for.

        Returns:
            The number of rows actually rescheduled.
        """
        ...

    def acknowledge_notification_attempt(self, check_id: int, attempt_seq: int) -> bool:
        """Clear the pending delivery marker of one notification attempt.

        Fenced by ``attempt_seq``: a row that an intervening OK sample cleared
        or a newer attempt advanced does not match, so a late acknowledgement
        cannot mark a different attempt as delivered.

        Args:
            check_id: The check that was notified about.
            attempt_seq: The sequence number the notification was sent for.

        Returns:
            Whether a row was changed.
        """
        ...

    def count_held_checks(self) -> int:
        """Return the number of checks with ``held_since > 0``."""
        ...

    async def count_held_checks_async(self) -> int:
        """Return the number of checks with ``held_since > 0``."""
        ...

    async def count_processing_claims_before_async(self, epoch: int) -> int:
        """Count executions claimed before ``epoch`` that are still in flight.

        Args:
            epoch: Exclusive upper bound on ``processing_started_at``.

        Returns:
            Number of ``health_check`` rows with ``status = 'processing'`` and
            a claim timestamp strictly before ``epoch``.
        """
        ...


class ServiceRepository(Protocol):
    """A repository interface for storing and retrieving services."""

    seen: set

    def add(self, service) -> None:
        """Add a service to the repository."""
        ...

    def get(self, service_id: int):
        """Get a service from the repository by ID."""
        ...

    def list(self) -> List[Service]:
        """Get a list of all services."""
        ...


Repository: TypeAlias = ResultRepository | CheckRepository | ServiceRepository


class RepositoryStore(Protocol):
    """A protocol for a collection of repositories."""

    results: ResultRepository
    checks: CheckRepository
    services: ServiceRepository

    def fork_for_concurrent_uow(self) -> "RepositoryStore":
        """Return a store view with independent event-tracking state."""
        ...

    def persist_check_result(
        self,
        check: Check,
        result: Result,
        notification_transition: NotificationTransition | None,
        *,
        complete_check: bool = True,
        hold_marker: int | None = None,
    ) -> bool:
        """Persist a result atomically; return false if its check was deleted.

        Args:
            check: The check the result belongs to.
            result: The sample to store.
            notification_transition: Expected and next notification state for
                the compare-and-swap, or ``None`` to persist without one.
            complete_check: Whether to release the processing claim.
            hold_marker: Epoch to stamp into ``held_since`` when it is still
                ``0``, in the same transaction. Used by the conflict-exhaustion
                fallback, which persists a held sample without a transition, so
                the recheck obligation cannot be separated from the completion
                by a crash. Ignored when a transition is given, and skipped
                when the completion was rejected as stale.
        """
        ...

    def get_collector_incident(self, incident_key: str) -> CollectorIncident | None:
        """Return the open collector incident for ``incident_key``, if any."""
        ...

    def claim_collector_incident_alert(
        self,
        incident_key: str,
        *,
        now: int,
        reminder_seconds: int,
        payload: dict[str, Any] | None = None,
    ) -> CollectorIncidentAlert:
        """Open or refresh a collector incident and claim the right to alert.

        Atomic: at most one caller (across iterations, threads, and process
        restarts) receives ``should_notify=True`` per reminder window.

        A granted claim additionally writes ``delivery_pending`` and
        ``delivery_attempt`` into the payload inside the same transaction, so
        a crash between the claim and the send leaves a durable retry intent.
        A claim that does not grant carries the existing markers forward.
        """
        ...

    def open_collector_incident(
        self, incident_key: str, *, now: int, payload: dict[str, Any]
    ) -> CollectorIncident:
        """Open a collector incident silently, or replace an open one's payload.

        The site-connectivity incident is opened as soon as an outage starts
        but must not alert before its notify-after threshold, which
        :meth:`claim_collector_incident_alert` cannot express: it claims the
        first alert in the same step. A row created here has
        ``last_alert_at = 0`` and ``alert_count = 0``, so the first later claim
        grants an alert.

        Args:
            incident_key: Identifier of the incident.
            now: Epoch used as ``opened_at`` for a newly created row.
            payload: Payload to store. On an existing row only the payload is
                replaced; ``opened_at``, ``last_alert_at`` and ``alert_count``
                are never touched, and the repository-owned delivery markers
                are carried forward.

        Returns:
            The resulting incident row.
        """
        ...

    def close_collector_incident(self, incident_key: str) -> CollectorIncident | None:
        """Close an incident, returning the state it had while open."""
        ...

    def acknowledge_collector_incident_delivery(
        self, incident_key: str, attempt: int
    ) -> bool:
        """Clear the delivery markers of one claimed collector-incident alert.

        Read and write happen in a single critical section (one
        ``BEGIN IMMEDIATE`` transaction in SQLite, the store lock in memory),
        which is what makes the acknowledgement safe against a claim granted
        concurrently: either the claim commits first and the stored
        ``delivery_attempt`` no longer matches, so nothing is cleared, or the
        acknowledgement commits first and the claim rewrites its own, newer
        intent afterwards. Either way the newest intent survives.

        No payload is supplied by the caller. Only ``delivery_pending`` and
        ``delivery_attempt`` are removed; every other key keeps the value it
        has in the row at acknowledgement time, so incident details written
        between the claim and the send are never rolled back.

        Args:
            incident_key: The incident whose alert was delivered.
            attempt: The ``alert_count`` the acknowledged claim granted.

        Returns:
            Whether the markers were actually cleared. ``False`` means the
            incident is gone or already carries a different attempt.
        """
        ...

    def set_collector_incident_payload(
        self, incident_key: str, payload: dict
    ) -> CollectorIncident | None:
        """Replace an open incident's payload without claiming an alert.

        Used to persist delivery state. Alert cadence fields (``last_alert_at``,
        ``alert_count``) are deliberately untouched, so recording a failed send
        cannot extend or shorten the reminder window.
        """
        ...

    def list(self) -> List[Repository]:
        """Get a list of all repositories."""
        ...
