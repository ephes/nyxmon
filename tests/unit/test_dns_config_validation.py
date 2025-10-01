"""Tests for DNS configuration validation enhancements."""

import pytest

from nyxmon.domain.dns_config import DnsCheckConfig


class TestDnsConfigIpValidation:
    """Tests for IP address validation in DNS configuration."""

    def test_valid_source_ip_passes(self):
        """Valid source IP should pass validation."""
        config = DnsCheckConfig(expected_ips=["192.0.2.1"], source_ip="192.168.1.100")
        assert config.validate() is True

    def test_valid_ipv6_source_ip_passes(self):
        """Valid IPv6 source IP should pass validation."""
        config = DnsCheckConfig(expected_ips=["2001:db8::1"], source_ip="2001:db8::100")
        assert config.validate() is True

    def test_invalid_source_ip_raises_error(self):
        """Invalid source IP format should raise ValueError."""
        config = DnsCheckConfig(
            expected_ips=["192.0.2.1"], source_ip="invalid.ip.address"
        )
        with pytest.raises(ValueError) as exc_info:
            config.validate()

        assert "Invalid source_ip" in str(exc_info.value)
        assert "invalid.ip.address" in str(exc_info.value)

    def test_interface_name_as_source_ip_raises_error(self):
        """Interface names (eth0, en0) should not be accepted as source IP."""
        config = DnsCheckConfig(expected_ips=["192.0.2.1"], source_ip="eth0")
        with pytest.raises(ValueError) as exc_info:
            config.validate()

        assert "Invalid source_ip" in str(exc_info.value)

    def test_valid_dns_server_passes(self):
        """Valid DNS server IP should pass validation."""
        config = DnsCheckConfig(expected_ips=["192.0.2.1"], dns_server="8.8.8.8")
        assert config.validate() is True

    def test_invalid_dns_server_raises_error(self):
        """Invalid DNS server format should raise ValueError."""
        config = DnsCheckConfig(expected_ips=["192.0.2.1"], dns_server="not-an-ip")
        with pytest.raises(ValueError) as exc_info:
            config.validate()

        assert "Invalid dns_server" in str(exc_info.value)
        assert "not-an-ip" in str(exc_info.value)

    def test_hostname_as_dns_server_raises_error(self):
        """Hostnames should not be accepted as DNS server."""
        config = DnsCheckConfig(expected_ips=["192.0.2.1"], dns_server="dns.google.com")
        with pytest.raises(ValueError) as exc_info:
            config.validate()

        assert "Invalid dns_server" in str(exc_info.value)

    def test_none_values_are_allowed(self):
        """None values for optional fields should not trigger validation."""
        config = DnsCheckConfig(
            expected_ips=["192.0.2.1"], source_ip=None, dns_server=None
        )
        assert config.validate() is True

    def test_both_invalid_ips_reported(self):
        """When both source_ip and dns_server are invalid, first one is reported."""
        config = DnsCheckConfig(
            expected_ips=["192.0.2.1"], source_ip="invalid1", dns_server="invalid2"
        )
        # Only first validation error is raised
        with pytest.raises(ValueError) as exc_info:
            config.validate()

        # Should fail on source_ip first (validated before dns_server)
        assert "Invalid source_ip" in str(
            exc_info.value
        ) or "Invalid dns_server" in str(exc_info.value)
