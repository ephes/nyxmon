"""HTTP check configuration domain model."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


DEFAULT_RETRY_STATUS_CODES = [502, 503, 504]


@dataclass
class HttpCheckConfig:
    """Typed configuration for plain HTTP checks."""

    timeout: float = 10.0
    retries: int = 0
    retry_delay: float = 2.0
    retry_status_codes: list[int] = field(
        default_factory=lambda: DEFAULT_RETRY_STATUS_CODES.copy()
    )
    follow_redirects: bool = True
    expected_status: int | None = None
    expected_location: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> "HttpCheckConfig":
        """Deserialize from check.data dictionary."""
        if not isinstance(data, dict):
            raise ValueError("check.data must be an object")

        raw_retry_status_codes = (
            data["retry_status_codes"]
            if "retry_status_codes" in data
            else DEFAULT_RETRY_STATUS_CODES
        )
        if not isinstance(raw_retry_status_codes, list | tuple | set):
            raise ValueError("retry_status_codes must be a list of HTTP status codes")
        follow_redirects = data.get("follow_redirects", True)
        if not isinstance(follow_redirects, bool):
            raise ValueError("follow_redirects must be a boolean")
        expected_location = data.get("expected_location")
        if expected_location is not None and not isinstance(expected_location, str):
            raise ValueError("expected_location must be a string")

        config = cls(
            timeout=data.get("timeout", 10.0),
            retries=data.get("retries", 0),
            retry_delay=data.get("retry_delay", 2.0),
            retry_status_codes=list(raw_retry_status_codes),
            follow_redirects=follow_redirects,
            expected_status=data.get("expected_status"),
            expected_location=expected_location,
        )
        config.validate()
        return config

    def to_dict(self) -> dict[str, Any]:
        """Serialize to check.data dictionary."""
        return {
            "timeout": self.timeout,
            "retries": self.retries,
            "retry_delay": self.retry_delay,
            "retry_status_codes": self.retry_status_codes,
            "follow_redirects": self.follow_redirects,
            "expected_status": self.expected_status,
            "expected_location": self.expected_location,
        }

    def validate(self) -> bool:
        """Validate configuration values."""
        for name, duration in (
            ("timeout", self.timeout),
            ("retry_delay", self.retry_delay),
        ):
            # Check the bound first to avoid converting huge integers to float.
            if (
                type(duration) not in (int, float)
                or not 0 <= duration <= 300
                or not math.isfinite(duration)
                or (name == "timeout" and duration == 0)
            ):
                raise ValueError(
                    f"{name} must be a finite number "
                    f"{'greater than zero and ' if name == 'timeout' else ''}"
                    "between 0 and 300 seconds"
                )
        if type(self.retries) is not int or not 0 <= self.retries <= 100:
            raise ValueError("retries must be an integer between 0 and 100")
        if not isinstance(self.retry_status_codes, list | tuple | set):
            raise ValueError("retry_status_codes must be a list of HTTP status codes")
        for status in self.retry_status_codes:
            if type(status) is not int or not 100 <= status <= 599:
                raise ValueError(
                    "retry_status_codes must contain integer HTTP status codes"
                )
        if self.expected_status is not None and (
            type(self.expected_status) is not int
            or not 100 <= self.expected_status <= 599
        ):
            raise ValueError("expected_status must be an integer HTTP status code")
        if self.expected_location is not None:
            if not self.expected_location:
                raise ValueError("expected_location must not be empty")
            if self.follow_redirects:
                raise ValueError(
                    "expected_location requires follow_redirects to be false"
                )

        return True
