from dataclasses import dataclass

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Result


class Event:
    """Base class for all events."""


@dataclass
class CheckGotResult(Event):
    """Check executed."""

    check_id: int
    result: "Result"


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
