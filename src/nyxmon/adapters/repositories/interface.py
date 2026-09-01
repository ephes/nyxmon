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
    """

    failure_count: int = 0
    last_attempt_count: int = 0
    last_immediate_at: int = 0
    last_notified_at: int = 0
    first_failure_at: int = 0

    @classmethod
    def from_row(cls, row: "Sequence[Any] | None") -> "NotificationState":
        """Build a state from a database row, tolerating pre-upgrade rows."""
        if row is None:
            return cls()
        values = [int(value or 0) for value in row[:5]]
        values.extend([0] * (5 - len(values)))
        return cls(*values)

    def as_row(self) -> tuple[int, int, int, int, int]:
        return (
            self.failure_count,
            self.last_attempt_count,
            self.last_immediate_at,
            self.last_notified_at,
            self.first_failure_at,
        )

    def cleared(self) -> "NotificationState":
        """Full reset used when a check recovers."""
        return NotificationState()

    def with_streak_reset(self) -> "NotificationState":
        """Reset the incident but keep the immediate-alert cooldown."""
        return NotificationState(last_immediate_at=self.last_immediate_at)

    def evolve(self, **changes: int) -> "NotificationState":
        return replace(self, **changes)


NOTIFICATION_STATE_COLUMNS = (
    "failure_count",
    "last_attempt_count",
    "last_immediate_at",
    "last_notified_at",
    "first_failure_at",
)

NotificationTransition: TypeAlias = tuple[NotificationState, NotificationState]


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
    ) -> bool:
        """Persist a result atomically; return false if its check was deleted."""
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
        """
        ...

    def close_collector_incident(self, incident_key: str) -> CollectorIncident | None:
        """Close an incident, returning the state it had while open."""
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
