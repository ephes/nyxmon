from ...domain import Result, Check, Service
from .interface import (
    Repository,
    ResultRepository,
    CheckRepository,
    ServiceRepository,
    RepositoryStore,
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

    def list(self) -> list[Result]:
        return list(self.results.values())

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
        self.seen: set[Check] = set()

    def add(self, check: Check) -> None:
        self.checks[check.check_id] = check
        self.seen.add(check)

    def get(self, check_id: int) -> Check:
        return self.checks[check_id]

    def list(self) -> list[Check]:
        return list(self.checks.values())


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

    def list(self) -> list[Service]:
        return list(self.services.values())


class InMemoryStore(RepositoryStore):
    """An in-memory store for the repositories."""

    def __init__(
        self,
        *,
        results: InMemoryResultRepository = InMemoryResultRepository(),
        checks: InMemoryCheckRepository = InMemoryCheckRepository(),
        services: InMemoryServiceRepository = InMemoryServiceRepository(),
    ) -> None:
        self.results = results
        self.checks = checks
        self.services = services

    def list(self) -> list[Repository]:
        return [
            self.results,
            self.checks,
            self.services,
        ]
