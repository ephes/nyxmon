from __future__ import annotations

from dataclasses import dataclass, field
import copy

from typing import List
import time

from ...domain import Check, CheckStatus, Result, Service
from .interface import (
    Repository,
    ResultRepository,
    CheckRepository,
    ServiceRepository,
    RepositoryStore,
    NotificationStateConflict,
    check_batch_size,
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
    """An in-memory implementation of the CheckRepository interface."""

    def __init__(self) -> None:
        self.checks: dict[int, Check] = {}
        self.notification_states: dict[int, tuple[int, int, int]] = {}
        self.seen: set[Check] = set()

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

    def get_notification_state(self, check_id: int) -> tuple[int, int, int]:
        return self.notification_states.get(check_id, (0, 0, 0))

    def set_notification_state(
        self,
        check_id: int,
        failure_count: int,
        last_attempt_count: int,
        last_immediate_at: int,
    ) -> None:
        self.notification_states[check_id] = (
            failure_count,
            last_attempt_count,
            last_immediate_at,
        )


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
    """An in-memory store for the repositories."""

    results: InMemoryResultRepository = field(default_factory=InMemoryResultRepository)
    checks: InMemoryCheckRepository = field(default_factory=InMemoryCheckRepository)
    services: InMemoryServiceRepository = field(
        default_factory=InMemoryServiceRepository
    )

    def fork_for_concurrent_uow(self) -> InMemoryStore:
        """Share backing data while isolating per-unit-of-work event sets."""
        results = InMemoryResultRepository()
        results.results = self.results.results
        results._timestamps = self.results._timestamps
        checks = InMemoryCheckRepository()
        checks.checks = self.checks.checks
        checks.notification_states = self.checks.notification_states
        services = InMemoryServiceRepository()
        services.services = self.services.services
        return InMemoryStore(results=results, checks=checks, services=services)

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
                self.checks.set_notification_state(check.check_id, *notification_state)
        return True

    def list(self) -> List[Repository]:
        return [
            self.results,
            self.checks,
            self.services,
        ]
