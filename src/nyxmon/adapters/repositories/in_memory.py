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

    def add(self, result: Result) -> None:
        if result.result_id is None:
            result.result_id = len(self.results)
        self.results[result.result_id] = result
        self.seen.add(result)

    def get(self, result_id: int) -> Result:
        return self.results[result_id]

    def list(self) -> list[Result]:
        return list(self.results.values())


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
