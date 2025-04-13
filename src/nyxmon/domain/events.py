from dataclasses import dataclass


class Event:
    """Base class for all events."""


@dataclass
class CheckExecuted(Event):
    """Check executed."""

    check_id: str
    result: bool


@dataclass
class CheckSucceeded(Event):
    """Check succeeded."""

    check_id: str


@dataclass
class CheckFailed(Event):
    """Check failed."""

    check_id: str
    result: bool


@dataclass
class ServiceStatusChanged(Event):
    """Service status changed."""

    service_id: str
    status: str
