"""Unit tests for TcpCheckConfig."""

import pytest

from nyxmon.domain.tcp_config import TcpCheckConfig


class TestTcpCheckConfig:
    """Tests for TcpCheckConfig domain model."""

    def test_from_dict_defaults(self) -> None:
        config = TcpCheckConfig.from_dict({"port": 25})

        assert config.port == 25
        assert config.host is None
        assert config.tls_mode == "none"
        assert config.connect_timeout == 10.0
        assert config.tls_handshake_timeout == 10.0
        assert config.retries == 1
        assert config.retry_delay == 0.0
        assert config.check_cert_expiry is False
        assert config.min_cert_days == 14
        assert config.starttls_command == "STARTTLS\r\n"
        assert config.verify is True

    def test_from_dict_with_overrides(self) -> None:
        data = {
            "port": 993,
            "host": "imap.example.com",
            "tls_mode": "implicit",
            "connect_timeout": 5.0,
            "tls_handshake_timeout": 7.5,
            "retries": 2,
            "retry_delay": 1.2,
            "check_cert_expiry": True,
            "min_cert_days": 21,
            "sni": "override.example.com",
            "starttls_command": "STLS\r\n",
            "verify": False,
        }

        config = TcpCheckConfig.from_dict(data)

        assert config.port == 993
        assert config.host == "imap.example.com"
        assert config.tls_mode == "implicit"
        assert config.connect_timeout == 5.0
        assert config.tls_handshake_timeout == 7.5
        assert config.retries == 2
        assert config.retry_delay == 1.2
        assert config.check_cert_expiry is True
        assert config.min_cert_days == 21
        assert config.sni == "override.example.com"
        assert config.starttls_command == "STLS\r\n"
        assert config.verify is False

    def test_to_dict_roundtrip(self) -> None:
        config = TcpCheckConfig(
            port=443,
            host="webmail.example.com",
            tls_mode="implicit",
            connect_timeout=3.0,
            tls_handshake_timeout=4.0,
            retries=0,
            retry_delay=0.5,
            check_cert_expiry=True,
            min_cert_days=5,
            sni="override.example.com",
            starttls_command="STARTTLS\r\n",
            verify=False,
        )

        restored = TcpCheckConfig.from_dict(config.to_dict())

        assert restored.port == 443
        assert restored.host == "webmail.example.com"
        assert restored.tls_mode == "implicit"
        assert restored.connect_timeout == 3.0
        assert restored.tls_handshake_timeout == 4.0
        assert restored.retries == 0
        assert restored.retry_delay == 0.5
        assert restored.check_cert_expiry is True
        assert restored.min_cert_days == 5
        assert restored.sni == "override.example.com"
        assert restored.verify is False

    def test_missing_port_raises(self) -> None:
        with pytest.raises(ValueError, match="port is required"):
            TcpCheckConfig.from_dict({})

    @pytest.mark.parametrize("invalid_mode", ["", "tls", "https"])
    def test_invalid_tls_mode(self, invalid_mode: str) -> None:
        config = TcpCheckConfig(port=25, tls_mode=invalid_mode)

        with pytest.raises(ValueError, match="tls_mode"):
            config.validate()

    @pytest.mark.parametrize(
        "field, value", [("connect_timeout", 0), ("tls_handshake_timeout", -1)]
    )
    def test_invalid_timeouts(self, field: str, value: float) -> None:
        config = TcpCheckConfig(port=25)
        setattr(config, field, value)

        with pytest.raises(ValueError):
            config.validate()

    def test_retry_and_cert_limits(self) -> None:
        config = TcpCheckConfig(
            port=25,
            retries=-1,
            retry_delay=-0.5,
            min_cert_days=-1,
        )

        with pytest.raises(ValueError):
            config.validate()

    def test_port_bounds_message_uses_constant(self) -> None:
        config = TcpCheckConfig(port=TcpCheckConfig.MAX_PORT + 1)

        with pytest.raises(ValueError, match=str(TcpCheckConfig.MAX_PORT)):
            config.validate()

    def test_starttls_requires_command(self) -> None:
        config = TcpCheckConfig(port=25, tls_mode="starttls", starttls_command="")

        with pytest.raises(ValueError, match="starttls_command"):
            config.validate()
