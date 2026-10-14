import threading
from dataclasses import dataclass, field

from .models import Check, CheckResult


class Command:
    """Base class for all commands."""


@dataclass
class IntakeStop:
    """Shutdown handshake between the collector and a running batch.

    The collector sets ``requested`` when the shutdown grace period ends. The
    runner then stops executing checks, hands every result that finished
    before the request to the result callback, and sets ``drained``.
    """

    requested: threading.Event = field(default_factory=threading.Event)
    drained: threading.Event = field(default_factory=threading.Event)


@dataclass
class ExecuteChecks(Command):
    """Run all pending checks."""

    checks: list[Check] = field(default_factory=list)
    intake_stop: IntakeStop | None = None


@dataclass
class RegisterCheck(Command):
    """Register a check."""

    check_id: int
    check_type: str
    check_data: dict


@dataclass
class DeleteCheck(Command):
    """Delete a check."""

    check_id: int


@dataclass
class AddCheck(Command):
    """Add a check."""

    check: Check


@dataclass
class AddCheckResult(Command):
    """Add a result for a check."""

    check_result: CheckResult


@dataclass
class StartCollector(Command):
    """Start the collector."""

    pass


@dataclass
class StopCollector(Command):
    """Stop the collector."""

    pass


@dataclass
class StartCleaner(Command):
    """Start the results cleaner."""

    pass


@dataclass
class StopCleaner(Command):
    """Stop the results cleaner."""

    pass
