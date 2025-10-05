from typing import List, Protocol, TypeAlias

from ...domain import Result, Check, Service


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

    def list(self) -> List[Repository]:
        """Get a list of all repositories."""
        ...
