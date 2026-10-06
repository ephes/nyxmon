"""Ping check configuration domain model."""

import math
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse


def _number(data: dict, name: str, default: float) -> float:
    """Read a finite number from ``data``, rejecting booleans and junk."""
    value: Any = data.get(name, default)
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be a finite number")
    return parsed


def normalize_ping_target(value: str | None) -> Optional[str]:
    """Return the host or IP address a ping check targets, or ``None``.

    Accepts a bare host name or IP address, a bracketed IPv6 literal, or a URL
    (whose host is used). Values that are empty, contain whitespace or start
    with ``-`` are rejected so a target can never be read as a ``ping`` option.
    """
    candidate = (value or "").strip()
    if "://" in candidate:
        try:
            candidate = urlparse(candidate).hostname or ""
        except ValueError:
            return None
    elif candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    candidate = candidate.strip()
    if (
        not candidate
        or candidate.startswith("-")
        or any(ch.isspace() for ch in candidate)
    ):
        return None
    return candidate


@dataclass
class PingCheckConfig:
    """Typed configuration for ICMP ping checks.

    The target host comes from ``check.url`` unless ``host`` is set in
    ``check.data``. Every attempt sends one echo request and waits at most
    ``timeout`` seconds for its reply; attempts are separated by ``interval``
    seconds.
    """

    MAX_COUNT = 20
    MAX_TIMEOUT = 60.0
    MAX_INTERVAL = 60.0

    timeout: float = 5.0
    count: int = 3
    interval: float = 1.0
    host: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "PingCheckConfig":
        """Deserialize from a ``check.data`` dictionary."""
        if not isinstance(data, dict):
            raise ValueError("check data must be an object")

        count = _number(data, "count", 3)
        if not count.is_integer():
            raise ValueError("count must be a whole number")

        host = data.get("host")
        if host is not None and not isinstance(host, str):
            raise ValueError("host must be a string")

        return cls(
            timeout=_number(data, "timeout", 5.0),
            count=int(count),
            interval=_number(data, "interval", 1.0),
            host=(host.strip() or None) if host is not None else None,
        )

    def to_dict(self) -> dict:
        """Serialize to a ``check.data`` dictionary."""
        data: dict[str, Any] = {
            "timeout": self.timeout,
            "count": self.count,
            "interval": self.interval,
        }
        if self.host is not None:
            data["host"] = self.host
        return data

    def validate(self) -> bool:
        """Validate configuration values.

        Raises:
            ValueError: If a value is out of range.
        """
        if self.timeout <= 0 or self.timeout > self.MAX_TIMEOUT:
            raise ValueError(
                f"timeout must be greater than 0 and at most {self.MAX_TIMEOUT:g} seconds"
            )
        if self.count < 1 or self.count > self.MAX_COUNT:
            raise ValueError(f"count must be between 1 and {self.MAX_COUNT}")
        if self.interval < 0 or self.interval > self.MAX_INTERVAL:
            raise ValueError(
                f"interval must be between 0 and {self.MAX_INTERVAL:g} seconds"
            )
        return True
