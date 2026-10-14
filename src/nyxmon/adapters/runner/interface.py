from collections.abc import Callable
from typing import Protocol, Iterable

from ...domain import Check
from ...domain.commands import IntakeStop


class CheckRunner(Protocol):
    """A runner interface for executing health checks."""

    def run_all(
        self,
        checks: Iterable[Check],
        result_received: Callable,
        intake_stop: IntakeStop | None = None,
    ) -> None:
        """Run all checks.

        When ``intake_stop.requested`` is set, stop executing checks, pass the
        results that already finished to ``result_received`` and set
        ``intake_stop.drained``.
        """
        ...
