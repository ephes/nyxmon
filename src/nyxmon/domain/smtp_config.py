"""SMTP check configuration domain model."""

from dataclasses import dataclass
from typing import Literal, Optional


TLSMode = Literal["none", "starttls", "implicit"]
VALID_TLS_MODES: set[TLSMode] = {"none", "starttls", "implicit"}


@dataclass
class SmtpCheckConfig:
    """Typed configuration for SMTP checks."""

    host: str
    port: int = 587
    tls: TLSMode = "starttls"
    username: Optional[str] = None
    password: Optional[str] = None
    password_secret: Optional[str] = None
    from_addr: str = ""
    to_addr: str = ""
    subject_prefix: str = "[nyxmon]"
    timeout: float = 30.0
    retries: int = 2
    retry_delay: float = 5.0

    @classmethod
    def from_dict(cls, data: dict) -> "SmtpCheckConfig":
        """Deserialize from check.data dictionary."""
        if "host" not in data or not data["host"]:
            raise ValueError("host is required")

        return cls(
            host=data["host"],
            port=data.get("port", 587),
            tls=data.get("tls", "starttls"),
            username=data.get("username"),
            password=data.get("password"),
            password_secret=data.get("password_secret"),
            from_addr=data.get("from_addr", ""),
            to_addr=data.get("to_addr", ""),
            subject_prefix=data.get("subject_prefix", "[nyxmon]"),
            timeout=data.get("timeout", 30.0),
            retries=data.get("retries", 2),
            retry_delay=data.get("retry_delay", 5.0),
        )

    def validate(self) -> bool:
        """Validate the configuration."""
        if self.tls not in VALID_TLS_MODES:
            raise ValueError(
                f"Invalid tls mode: {self.tls}. Must be one of {sorted(VALID_TLS_MODES)}"
            )

        if self.port <= 0:
            raise ValueError("port must be positive")

        if not self.from_addr:
            raise ValueError("from_addr is required")

        if not self.to_addr:
            raise ValueError("to_addr is required")

        if not self.subject_prefix:
            raise ValueError("subject_prefix is required")

        if self.timeout <= 0:
            raise ValueError("timeout must be positive")

        if self.retries < 0:
            raise ValueError("retries must be zero or positive")

        if self.retry_delay < 0:
            raise ValueError("retry_delay must be zero or positive")

        if self.username and not (self.password or self.password_secret):
            raise ValueError(
                "password or password_secret is required when username is provided"
            )

        return True

    def get_password(self) -> Optional[str]:
        """Return the best-available password value."""
        if self.password:
            return self.password

        if self.password_secret:
            return self.password_secret

        return None
