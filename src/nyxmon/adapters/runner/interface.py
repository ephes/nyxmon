from collections.abc import Callable
from typing import Protocol, Iterable

from ...domain import Check


class CheckRunner(Protocol):
    """A runner interface for executing health checks."""

    def run_all(self, checks: Iterable[Check], result_received: Callable) -> None:
        """Run all checks."""
        ...
