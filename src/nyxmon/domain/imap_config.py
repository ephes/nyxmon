"""IMAP check configuration domain model."""

from dataclasses import dataclass

ALLOWED_TLS_MODES = {"implicit", "starttls", "none"}


@dataclass
class ImapCheckConfig:
    """Typed configuration for IMAP checks."""

    host: str
    username: str
    search_subject: str
    password: str | None = None
    password_secret: str | None = None
    folder: str = "INBOX"
    port: int = 993
    tls_mode: str = "implicit"  # implicit TLS by default
    max_age_minutes: int = 30
    delete_after_check: bool = True
    timeout: float = 30.0
    retries: int = 2
    retry_delay: float = 10.0

    @classmethod
    def from_dict(cls, data: dict) -> "ImapCheckConfig":
        """Deserialize from check.data dictionary."""
        host = data.get("host", "")
        username = data.get("username", "")
        password = data.get("password")
        password_secret = data.get("password_secret")
        search_subject = data.get("search_subject", "")
        tls_mode = data.get("tls_mode", "implicit")

        if not host:
            raise ValueError("host is required")
        if not username:
            raise ValueError("username is required")
        if not (password or password_secret):
            raise ValueError("password or password_secret is required")
        if not search_subject:
            raise ValueError("search_subject is required")
        if tls_mode not in ALLOWED_TLS_MODES:
            raise ValueError(f"tls_mode must be one of {ALLOWED_TLS_MODES}")

        return cls(
            host=host,
            username=username,
            password=password,
            password_secret=password_secret,
            search_subject=search_subject,
            folder=data.get("folder", "INBOX"),
            port=data.get("port", 993),
            tls_mode=tls_mode,
            max_age_minutes=data.get("max_age_minutes", 30),
            delete_after_check=data.get("delete_after_check", True),
            timeout=data.get("timeout", 30.0),
            retries=data.get("retries", 2),
            retry_delay=float(data.get("retry_delay", 10.0)),
        )

    def to_dict(self) -> dict:
        """Serialize to check.data dictionary."""
        return {
            "host": self.host,
            "username": self.username,
            "password": self.password,
            "search_subject": self.search_subject,
            "folder": self.folder,
            "port": self.port,
            "tls_mode": self.tls_mode,
            "max_age_minutes": self.max_age_minutes,
            "delete_after_check": self.delete_after_check,
            "timeout": self.timeout,
            "retries": self.retries,
            "retry_delay": self.retry_delay,
            "password_secret": self.password_secret,
        }

    def validate(self) -> bool:
        """Validate configuration values."""
        if self.max_age_minutes <= 0:
            raise ValueError("max_age_minutes must be positive")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.retries < 0:
            raise ValueError("retries must be zero or positive")
        if self.retry_delay < 0:
            raise ValueError("retry_delay must be zero or positive")
        if not self.host:
            raise ValueError("host is required")

        return True

    def resolved_password(self) -> str | None:
        """Return the best available password (plain or secret reference)."""
        return self.password or self.password_secret
