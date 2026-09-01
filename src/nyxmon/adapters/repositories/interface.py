import logging
import os
from functools import lru_cache
from typing import List, Protocol, TypeAlias

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

    def get_notification_state(self, check_id: int) -> tuple[int, int, int]:
        """Return failure count, last attempted count, and immediate-alert time."""
        ...

    def set_notification_state(
        self,
        check_id: int,
        failure_count: int,
        last_attempt_count: int,
        last_immediate_at: int,
    ) -> None:
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
        notification_transition: (
            tuple[tuple[int, int, int], tuple[int, int, int]] | None
        ),
        *,
        complete_check: bool = True,
    ) -> bool:
        """Persist a result atomically; return false if its check was deleted."""
        ...

    def list(self) -> List[Repository]:
        """Get a list of all repositories."""
        ...
