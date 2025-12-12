"""TCP check configuration domain model."""

from dataclasses import dataclass
from typing import Optional


ALLOWED_TLS_MODES = {"none", "implicit", "starttls"}


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
    verify: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "TcpCheckConfig":
        """Deserialize from check.data dictionary."""
        if "port" not in data:
            raise ValueError("port is required")

        return cls(
            port=int(data["port"]),
            host=data.get("host"),
            tls_mode=data.get("tls_mode", "none"),
            connect_timeout=float(data.get("connect_timeout", 10.0)),
            tls_handshake_timeout=float(data.get("tls_handshake_timeout", 10.0)),
            retries=int(data.get("retries", 1)),
            retry_delay=float(data.get("retry_delay", 0.0)),
            check_cert_expiry=bool(data.get("check_cert_expiry", False)),
            min_cert_days=int(data.get("min_cert_days", 14)),
            sni=data.get("sni"),
            starttls_command=data.get("starttls_command", "STARTTLS\r\n"),
            verify=bool(data.get("verify", True)),
        )

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
            "verify": self.verify,
        }

        if self.host is not None:
            data["host"] = self.host

        if self.sni is not None:
            data["sni"] = self.sni

        return data

    def validate(self) -> bool:
        """Validate configuration values."""
        if self.port <= 0 or self.port > self.MAX_PORT:
            raise ValueError(f"port must be between 1 and {self.MAX_PORT}")

        if self.tls_mode not in ALLOWED_TLS_MODES:
            raise ValueError(f"tls_mode must be one of {sorted(ALLOWED_TLS_MODES)}")

        if self.connect_timeout <= 0:
            raise ValueError("connect_timeout must be positive")

        if self.tls_handshake_timeout <= 0:
            raise ValueError("tls_handshake_timeout must be positive")

        if self.retries < 0:
            raise ValueError("retries must be zero or positive")

        if self.retry_delay < 0:
            raise ValueError("retry_delay must be zero or positive")

        if self.min_cert_days < 0:
            raise ValueError("min_cert_days must be zero or positive")

        if self.tls_mode == "starttls" and not self.starttls_command:
            raise ValueError("starttls_command is required for starttls mode")

        return True
