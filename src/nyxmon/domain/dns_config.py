"""DNS check configuration domain model."""

import ipaddress
from dataclasses import dataclass
from typing import List, Optional


VALID_QUERY_TYPES = {"A", "AAAA", "MX", "TXT", "CNAME", "NS", "SOA", "PTR"}

#: Address family each address query type answers with.
IP_QUERY_TYPE_VERSIONS = {"A": 4, "AAAA": 6}


def normalize_dns_value(value: str) -> str:
    """Return the canonical text of an IP address, or ``value`` unchanged.

    IP addresses are compared by value, not by spelling:
    ``2A01:04F8:0000::0001`` and ``2a01:4f8::1`` are the same address. Values
    that are not IP addresses (MX hosts, TXT strings) are returned as given so
    they keep their exact-match semantics.
    """
    try:
        return ipaddress.ip_address(value.strip()).compressed
    except ValueError:
        return value


def expected_ip_error(value: str, query_type: str) -> Optional[str]:
    """Explain why ``value`` can never match an answer to ``query_type``.

    Returns ``None`` when ``value`` is a literal IP address of the family the
    query type returns (IPv4 for ``A``, IPv6 for ``AAAA``), or when the query
    type is not an address query.
    """
    version = IP_QUERY_TYPE_VERSIONS.get(query_type)
    if version is None:
        return None
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return f"Invalid IP address: {value}"
    if address.version != version:
        return (
            f"{value} is an IPv{address.version} address, but a {query_type} "
            f"query only returns IPv{version} addresses"
        )
    return None


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
        # A bare string would otherwise be split into single characters by
        # every set() comparison downstream.
        if not isinstance(expected_ips, list) or not all(
            isinstance(ip, str) for ip in expected_ips
        ):
            raise ValueError("expected_ips must be a list of strings")
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

        # An expectation of the wrong address family can never match.
        for ip in self.expected_ips:
            error = expected_ip_error(ip, self.query_type)
            if error is not None:
                raise ValueError(f"Invalid expected_ips entry: {error}")

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
