"""JSON metrics check configuration domain model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


ALLOWED_OPERATORS = {"<", "<=", ">", ">=", "==", "!="}
ALLOWED_SEVERITIES = {"warning", "critical"}
# Largest response body the executor reads before failing with
# ``body_too_large``. Metrics documents are small; this bounds memory use when
# a check points at the wrong URL.
DEFAULT_MAX_BODY_BYTES = 1024 * 1024


@dataclass
class JsonMetricsCheckConfig:
    """Typed configuration for JSON metrics checks."""

    @dataclass
    class Check:
        path: str
        op: str
        value: Any
        severity: str

    url: str
    checks: List[Check]
    timeout: float = 10.0
    auth: Optional[Dict[str, str]] = None
    retries: int = 1
    retry_delay: float = 2.0
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES

    @classmethod
    def from_dict(cls, data: dict) -> "JsonMetricsCheckConfig":
        url = data.get("url", "")
        if not url:
            raise ValueError("url is required")

        raw_checks = data.get("checks", [])
        if not raw_checks:
            raise ValueError("checks must contain at least one entry")

        checks: list[JsonMetricsCheckConfig.Check] = []
        for entry in raw_checks:
            op = entry.get("op")
            severity = entry.get("severity")
            if op not in ALLOWED_OPERATORS:
                raise ValueError(f"op must be one of {ALLOWED_OPERATORS}")
            if severity not in ALLOWED_SEVERITIES:
                raise ValueError(f"severity must be one of {ALLOWED_SEVERITIES}")

            checks.append(
                cls.Check(
                    path=entry.get("path", ""),
                    op=op,
                    value=entry.get("value"),
                    severity=severity,
                )
            )

        auth = data.get("auth")
        if auth is not None:
            if "username" not in auth or "password" not in auth:
                raise ValueError("auth must include username and password")

        max_body_bytes = data.get("max_body_bytes", DEFAULT_MAX_BODY_BYTES)
        if max_body_bytes is None:
            max_body_bytes = DEFAULT_MAX_BODY_BYTES
        if isinstance(max_body_bytes, bool) or not isinstance(max_body_bytes, int):
            raise ValueError("max_body_bytes must be a whole number of bytes")

        return cls(
            url=url,
            timeout=data.get("timeout", 10.0),
            auth=auth,
            checks=checks,
            retries=int(data.get("retries", 1)),
            retry_delay=float(data.get("retry_delay", 2.0)),
            max_body_bytes=max_body_bytes,
        )

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "timeout": self.timeout,
            "auth": self.auth,
            "retries": self.retries,
            "retry_delay": self.retry_delay,
            "max_body_bytes": self.max_body_bytes,
            "checks": [
                {"path": c.path, "op": c.op, "value": c.value, "severity": c.severity}
                for c in self.checks
            ],
        }

    def validate(self) -> bool:
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.retries < 0:
            raise ValueError("retries must be zero or positive")
        if self.retry_delay < 0:
            raise ValueError("retry_delay must be zero or positive")
        if self.max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")

        for check in self.checks:
            if check.path == "":
                raise ValueError("check.path is required")
            if check.op not in ALLOWED_OPERATORS:
                raise ValueError(f"Invalid operator {check.op}")
            if check.severity not in ALLOWED_SEVERITIES:
                raise ValueError(f"Invalid severity {check.severity}")

        return True
