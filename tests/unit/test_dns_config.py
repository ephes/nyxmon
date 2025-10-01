"""Unit tests for DnsCheckConfig."""

import pytest
from nyxmon.domain.dns_config import DnsCheckConfig


class TestDnsCheckConfig:
    """Tests for DnsCheckConfig domain model."""

    def test_create_config_with_required_fields(self):
        """Should create config with only required fields."""
        config = DnsCheckConfig(expected_ips=["192.168.1.1"])

        assert config.expected_ips == ["192.168.1.1"]
        assert config.dns_server is None
        assert config.source_ip is None
        assert config.query_type == "A"
        assert config.timeout == 5.0

    def test_create_config_with_all_fields(self):
        """Should create config with all fields specified."""
        config = DnsCheckConfig(
            expected_ips=["192.168.1.1", "192.168.1.2"],
            dns_server="8.8.8.8",
            source_ip="192.168.1.100",
            query_type="AAAA",
            timeout=10.0,
        )

        assert config.expected_ips == ["192.168.1.1", "192.168.1.2"]
        assert config.dns_server == "8.8.8.8"
        assert config.source_ip == "192.168.1.100"
        assert config.query_type == "AAAA"
        assert config.timeout == 10.0

    def test_from_dict_with_required_fields(self):
        """Should deserialize from dict with only required fields."""
        data = {"expected_ips": ["192.168.178.94"]}

        config = DnsCheckConfig.from_dict(data)

        assert config.expected_ips == ["192.168.178.94"]
        assert config.dns_server is None
        assert config.source_ip is None
        assert config.query_type == "A"
        assert config.timeout == 5.0

    def test_from_dict_with_all_fields(self):
        """Should deserialize from dict with all fields."""
        data = {
            "expected_ips": ["192.168.178.94"],
            "dns_server": "192.168.178.94",
            "source_ip": "192.168.178.50",
            "query_type": "MX",
            "timeout": 3.0,
        }

        config = DnsCheckConfig.from_dict(data)

        assert config.expected_ips == ["192.168.178.94"]
        assert config.dns_server == "192.168.178.94"
        assert config.source_ip == "192.168.178.50"
        assert config.query_type == "MX"
        assert config.timeout == 3.0

    def test_from_dict_missing_required_field(self):
        """Should raise error when required field is missing."""
        data = {"dns_server": "8.8.8.8"}

        with pytest.raises(ValueError, match="expected_ips is required"):
            DnsCheckConfig.from_dict(data)

    def test_from_dict_empty_expected_ips(self):
        """Should raise error when expected_ips is empty."""
        data = {"expected_ips": []}

        with pytest.raises(ValueError, match="expected_ips cannot be empty"):
            DnsCheckConfig.from_dict(data)

    def test_to_dict(self):
        """Should serialize to dict correctly."""
        config = DnsCheckConfig(
            expected_ips=["192.168.1.1"],
            dns_server="8.8.8.8",
            source_ip="192.168.1.100",
        )

        data = config.to_dict()

        assert data == {
            "expected_ips": ["192.168.1.1"],
            "dns_server": "8.8.8.8",
            "source_ip": "192.168.1.100",
            "query_type": "A",
            "timeout": 5.0,
        }

    def test_to_dict_with_none_values(self):
        """Should not include None values in dict."""
        config = DnsCheckConfig(expected_ips=["192.168.1.1"])

        data = config.to_dict()

        assert data == {
            "expected_ips": ["192.168.1.1"],
            "query_type": "A",
            "timeout": 5.0,
        }
        assert "dns_server" not in data
        assert "source_ip" not in data

    def test_invalid_query_type_validation(self):
        """Should validate query type is valid DNS record type."""
        config = DnsCheckConfig(expected_ips=["192.168.1.1"])

        # Valid types should work
        for query_type in ["A", "AAAA", "MX", "TXT", "CNAME", "NS", "SOA", "PTR"]:
            config.query_type = query_type
            assert config.validate()

        # Invalid type should fail validation
        config.query_type = "INVALID"
        with pytest.raises(ValueError, match="Invalid query_type"):
            config.validate()

    def test_invalid_timeout_validation(self):
        """Should validate timeout is positive."""
        config = DnsCheckConfig(expected_ips=["192.168.1.1"])

        config.timeout = 0
        with pytest.raises(ValueError, match="Timeout must be positive"):
            config.validate()

        config.timeout = -1
        with pytest.raises(ValueError, match="Timeout must be positive"):
            config.validate()
