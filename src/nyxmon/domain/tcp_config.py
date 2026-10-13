"""TCP check configuration domain model."""

import math
from dataclasses import dataclass
from typing import Optional


ALLOWED_TLS_MODES = {"none", "implicit", "starttls"}
ALLOWED_STARTTLS_PROTOCOLS = {"generic", "smtp", "imap", "sieve"}


@dataclass
class TcpCheckConfig:
    """Typed configuration for TCP checks."""

    MAX_PORT = 65535

    port: int
    host: Optional[str] = None
    tls_mode: str = "none"
    connect_timeout: float = 10.0
    tls_handshake_timeout: float = 10.0
    retries: int = 1
    retry_delay: float = 0.0
    check_cert_expiry: bool = False
    min_cert_days: int = 14
    sni: Optional[str] = None
    starttls_command: str = "STARTTLS\r\n"
    starttls_protocol: str = "generic"
    starttls_read_greeting: bool = False
    verify: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "TcpCheckConfig":
        """Deserialize from check.data dictionary."""
        if "port" not in data:
            raise ValueError("port is required")

        config = cls(
            port=data["port"],
            host=data.get("host"),
            tls_mode=data.get("tls_mode", "none"),
            connect_timeout=data.get("connect_timeout", 10.0),
            tls_handshake_timeout=data.get("tls_handshake_timeout", 10.0),
            retries=data.get("retries", 1),
            retry_delay=data.get("retry_delay", 0.0),
            check_cert_expiry=data.get("check_cert_expiry", False),
            min_cert_days=data.get("min_cert_days", 14),
            sni=data.get("sni"),
            starttls_command=data.get("starttls_command", "STARTTLS\r\n"),
            starttls_protocol=data.get("starttls_protocol") or "generic",
            starttls_read_greeting=data.get("starttls_read_greeting", False),
            verify=data.get("verify", True),
        )

        config.validate()
        return config

    def to_dict(self) -> dict:
        """Serialize to check.data dictionary."""
        data = {
            "port": self.port,
            "tls_mode": self.tls_mode,
            "connect_timeout": self.connect_timeout,
            "tls_handshake_timeout": self.tls_handshake_timeout,
            "retries": self.retries,
            "retry_delay": self.retry_delay,
            "check_cert_expiry": self.check_cert_expiry,
            "min_cert_days": self.min_cert_days,
            "starttls_command": self.starttls_command,
            "starttls_protocol": self.starttls_protocol,
            "starttls_read_greeting": self.starttls_read_greeting,
            "verify": self.verify,
        }

        if self.host is not None:
            data["host"] = self.host

        if self.sni is not None:
            data["sni"] = self.sni

        return data

    def validate(self) -> bool:
        """Validate configuration values."""
        for name, boolean in (
            ("verify", self.verify),
            ("check_cert_expiry", self.check_cert_expiry),
            ("starttls_read_greeting", self.starttls_read_greeting),
        ):
            if type(boolean) is not bool:
                raise ValueError(f"{name} must be a boolean")

        for name, value, minimum, maximum in (
            ("port", self.port, 1, self.MAX_PORT),
            ("retries", self.retries, 0, 100),
            ("min_cert_days", self.min_cert_days, 0, 36500),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(
                    f"{name} must be an integer between {minimum} and {maximum}"
                )

        for name, duration in (
            ("connect_timeout", self.connect_timeout),
            ("tls_handshake_timeout", self.tls_handshake_timeout),
            ("retry_delay", self.retry_delay),
        ):
            # Check the bound first to avoid converting huge integers to float.
            if (
                type(duration) not in (int, float)
                or not 0 <= duration <= 300
                or not math.isfinite(duration)
                or (name != "retry_delay" and duration == 0)
            ):
                raise ValueError(
                    f"{name} must be a finite number "
                    f"{'greater than zero and ' if name != 'retry_delay' else ''}"
                    "between 0 and 300 seconds"
                )

        if self.tls_mode not in ALLOWED_TLS_MODES:
            raise ValueError(f"tls_mode must be one of {sorted(ALLOWED_TLS_MODES)}")

        if (
            not isinstance(self.starttls_protocol, str)
            or self.starttls_protocol not in ALLOWED_STARTTLS_PROTOCOLS
        ):
            raise ValueError(
                f"starttls_protocol must be one of {sorted(ALLOWED_STARTTLS_PROTOCOLS)}"
            )

        if (
            self.tls_mode == "starttls"
            and self.starttls_protocol == "generic"
            and not self.starttls_command
        ):
            raise ValueError("starttls_command is required for starttls mode")

        return True
