"""SMTP check configuration domain model."""

import math
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
    verify: bool = True  # verify TLS certificate and hostname

    @classmethod
    def from_dict(cls, data: dict) -> "SmtpCheckConfig":
        """Deserialize from check.data dictionary."""
        if "host" not in data or not data["host"]:
            raise ValueError("host is required")

        verify = data.get("verify", True)
        if verify is None:
            verify = True
        if not isinstance(verify, bool):
            raise ValueError("verify must be a boolean")

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
            verify=verify,
        )

    def validate(self) -> bool:
        """Validate the configuration."""
        if self.tls not in VALID_TLS_MODES:
            raise ValueError(
                f"Invalid tls mode: {self.tls}. Must be one of {sorted(VALID_TLS_MODES)}"
            )

        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("port must be an integer between 1 and 65535")

        if type(self.retries) is not int or self.retries < 0:
            raise ValueError("retries must be a nonnegative integer")

        for name, value in (
            ("timeout", self.timeout),
            ("retry_delay", self.retry_delay),
        ):
            # Check the bound before isfinite so huge integers cannot overflow
            # its conversion to float. Booleans are not numeric configuration.
            if (
                type(value) not in (int, float)
                or not 0 <= value <= 300
                or not math.isfinite(value)
                or (name == "timeout" and value == 0)
            ):
                raise ValueError(
                    f"{name} must be a finite number "
                    f"{'greater than zero and ' if name == 'timeout' else ''}"
                    "between 0 and 300 seconds"
                )

        for name, header in (
            ("from_addr", self.from_addr),
            ("to_addr", self.to_addr),
            ("subject_prefix", self.subject_prefix),
        ):
            if (
                not isinstance(header, str)
                or not header.strip()
                or "\r" in header
                or "\n" in header
            ):
                raise ValueError(f"{name} must be a nonempty string without CR/LF")

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
