from dataclasses import dataclass


class Command:
    """Base class for all commands."""


@dataclass
class ExecuteChecks(Command):
    """Run all pending checks."""

    ...


@dataclass
class RegisterCheck(Command):
    """Register a check."""

    check_id: str
    check_type: str
    check_data: dict


@dataclass
class DeleteCheck(Command):
    """Delete a check."""

    check_id: str
