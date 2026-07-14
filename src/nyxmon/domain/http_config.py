"""HTTP check configuration domain model."""

from __future__ import annotations

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

        try:
            raw_expected_status = data.get("expected_status")
            return cls(
                timeout=float(data.get("timeout", 10.0)),
                retries=int(data.get("retries", 0)),
                retry_delay=float(data.get("retry_delay", 2.0)),
                retry_status_codes=[int(status) for status in raw_retry_status_codes],
                follow_redirects=follow_redirects,
                expected_status=(
                    int(raw_expected_status)
                    if raw_expected_status is not None
                    else None
                ),
                expected_location=expected_location,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid HTTP check configuration") from exc

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
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.retries < 0:
            raise ValueError("retries must be zero or positive")
        if self.retry_delay < 0:
            raise ValueError("retry_delay must be zero or positive")
        for status in self.retry_status_codes:
            if status < 100 or status > 599:
                raise ValueError("retry_status_codes must contain HTTP status codes")
        if self.expected_status is not None and not 100 <= self.expected_status <= 599:
            raise ValueError("expected_status must be an HTTP status code")
        if self.expected_location is not None:
            if not self.expected_location:
                raise ValueError("expected_location must not be empty")
            if self.follow_redirects:
                raise ValueError(
                    "expected_location requires follow_redirects to be false"
                )

        return True
