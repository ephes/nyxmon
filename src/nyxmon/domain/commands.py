from dataclasses import dataclass, field

from .models import Check, Result


class Command:
    """Base class for all commands."""


@dataclass
class ExecuteChecks(Command):
    """Run all pending checks."""

    checks: list[Check] = field(default_factory=list)


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
class AddResult(Command):
    """Add a result for a check."""

    check: Check
    result: Result


@dataclass
class StartCollector(Command):
    """Start the collector."""

    pass


@dataclass
class StopCollector(Command):
    """Stop the collector."""

    pass
