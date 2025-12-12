"""Unit tests for ImapCheckConfig."""

import pytest

from nyxmon.domain.imap_config import ImapCheckConfig


class TestImapCheckConfig:
    """Tests for ImapCheckConfig domain model."""

    def test_from_dict_with_required_fields(self) -> None:
        data = {
            "host": "imap.example.com",
            "username": "user",
            "password": "secret",
            "search_subject": "[nyxmon]",
        }

        config = ImapCheckConfig.from_dict(data)

        assert config.username == "user"
        assert config.password == "secret"
        assert config.search_subject == "[nyxmon]"
        assert config.folder == "INBOX"
        assert config.port == 993
        assert config.tls_mode == "implicit"
        assert config.max_age_minutes == 30
        assert config.delete_after_check is True
        assert config.retries == 2
        assert config.retry_delay == 10

    def test_from_dict_with_overrides(self) -> None:
        data = {
            "host": "imap.example.com",
            "username": "user",
            "password": "secret",
            "search_subject": "[nyxmon]",
            "folder": "Mail/Nyxmon",
            "port": 143,
            "tls_mode": "starttls",
            "max_age_minutes": 15,
            "delete_after_check": False,
            "timeout": 20.0,
            "retries": 1,
            "retry_delay": 5,
        }

        config = ImapCheckConfig.from_dict(data)

        assert config.folder == "Mail/Nyxmon"
        assert config.port == 143
        assert config.tls_mode == "starttls"
        assert config.max_age_minutes == 15
        assert config.delete_after_check is False
        assert config.timeout == 20.0
        assert config.retries == 1
        assert config.retry_delay == 5

    def test_to_dict_roundtrip(self) -> None:
        config = ImapCheckConfig(
            host="imap.example.com",
            username="user",
            password="secret",
            search_subject="[nyxmon]",
            folder="INBOX",
            port=993,
            tls_mode="implicit",
            max_age_minutes=45,
            delete_after_check=False,
            timeout=25.0,
            retries=3,
            retry_delay=7.5,
            password_secret="secret-ref",
        )

        data = config.to_dict()
        restored = ImapCheckConfig.from_dict(data)

        assert restored.username == "user"
        assert restored.password == "secret"
        assert restored.search_subject == "[nyxmon]"
        assert restored.host == "imap.example.com"
        assert restored.max_age_minutes == 45
        assert restored.delete_after_check is False
        assert restored.timeout == 25.0
        assert restored.retries == 3
        assert restored.retry_delay == 7.5
        assert restored.password_secret == "secret-ref"

    @pytest.mark.parametrize(
        "field, value, message",
        [
            ("host", "", "host is required"),
            ("username", "", "username is required"),
            ("password", None, "password or password_secret is required"),
            ("search_subject", "", "search_subject is required"),
        ],
    )
    def test_missing_required_fields(
        self, field: str, value: str, message: str
    ) -> None:
        data = {
            "host": "imap.example.com",
            "username": "user",
            "password": "secret",
            "search_subject": "[nyxmon]",
        }
        data[field] = value

        with pytest.raises(ValueError, match=message):
            ImapCheckConfig.from_dict(data)

    @pytest.mark.parametrize("invalid", ["", "unknown", "SSL"])
    def test_invalid_tls_mode(self, invalid: str) -> None:
        data = {
            "host": "imap.example.com",
            "username": "user",
            "password": "secret",
            "search_subject": "[nyxmon]",
            "tls_mode": invalid,
        }

        with pytest.raises(ValueError, match="tls_mode"):
            ImapCheckConfig.from_dict(data)

    def test_validate_numeric_limits(self) -> None:
        config = ImapCheckConfig(
            host="imap.example.com",
            username="user",
            password="secret",
            search_subject="[nyxmon]",
            max_age_minutes=0,
            timeout=0,
            retries=-1,
            retry_delay=-5,
        )

        with pytest.raises(ValueError):
            config.validate()
