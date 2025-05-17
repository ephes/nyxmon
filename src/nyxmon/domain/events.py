from dataclasses import dataclass


class Event:
    """Base class for all events."""


@dataclass
class CheckSucceeded(Event):
    """Check succeeded."""

    check_id: int


@dataclass
class CheckFailed(Event):
    """Check failed."""

    check_id: int
    result: bool


@dataclass
class ServiceStatusChanged(Event):
    """Service status changed."""

    service_id: int
    status: str
