"""DNS check configuration domain model."""

import ipaddress
from dataclasses import dataclass
from typing import List, Optional


VALID_QUERY_TYPES = {"A", "AAAA", "MX", "TXT", "CNAME", "NS", "SOA", "PTR"}


@dataclass
class DnsCheckConfig:
    """Typed configuration for DNS checks.

    Attributes:
        expected_ips: List of IP addresses we expect to receive (required)
        dns_server: DNS server to query (uses system default if not specified)
        source_ip: Source IP address to bind for the query (not interface name)
        query_type: DNS record type (default: "A")
        timeout: Query timeout in seconds (default: 5.0)
    """

    expected_ips: List[str]
    dns_server: Optional[str] = None
    source_ip: Optional[str] = None
    query_type: str = "A"
    timeout: float = 5.0

    @classmethod
    def from_dict(cls, data: dict) -> "DnsCheckConfig":
        """Deserialize from check.data dictionary.

        Args:
            data: Dictionary containing DNS check configuration

        Returns:
            DnsCheckConfig instance

        Raises:
            ValueError: If required fields are missing or invalid
        """
        if "expected_ips" not in data:
            raise ValueError("expected_ips is required")

        expected_ips = data["expected_ips"]
        if not expected_ips:
            raise ValueError("expected_ips cannot be empty")

        return cls(
            expected_ips=expected_ips,
            dns_server=data.get("dns_server"),
            source_ip=data.get("source_ip"),
            query_type=data.get("query_type", "A"),
            timeout=data.get("timeout", 5.0),
        )

    def to_dict(self) -> dict:
        """Serialize to check.data dictionary.

        Returns:
            Dictionary representation suitable for storage in check.data
        """
        result = {
            "expected_ips": self.expected_ips,
            "query_type": self.query_type,
            "timeout": self.timeout,
        }

        if self.dns_server is not None:
            result["dns_server"] = self.dns_server

        if self.source_ip is not None:
            result["source_ip"] = self.source_ip

        return result

    def validate(self) -> bool:
        """Validate the configuration.

        Returns:
            True if valid

        Raises:
            ValueError: If configuration is invalid
        """
        if self.query_type not in VALID_QUERY_TYPES:
            raise ValueError(
                f"Invalid query_type: {self.query_type}. Must be one of {VALID_QUERY_TYPES}"
            )

        if self.timeout <= 0:
            raise ValueError("Timeout must be positive")

        # Validate source IP address format if provided
        if self.source_ip:
            try:
                ipaddress.ip_address(self.source_ip)
            except ValueError as e:
                raise ValueError(
                    f"Invalid source_ip: {self.source_ip}. Must be a valid IP address."
                ) from e

        # Validate DNS server IP address format if provided
        if self.dns_server:
            try:
                ipaddress.ip_address(self.dns_server)
            except ValueError as e:
                raise ValueError(
                    f"Invalid dns_server: {self.dns_server}. Must be a valid IP address."
                ) from e

        return True
