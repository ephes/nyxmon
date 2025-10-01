"""End-to-end tests for DNS checks."""

from unittest.mock import patch, MagicMock, AsyncMock
import dns.rdatatype
import dns.resolver

from nyxmon.bootstrap import bootstrap
from nyxmon.domain import Check, CheckType, ResultStatus
from nyxmon.domain.commands import ExecuteChecks


class MockDnsResolver:
    """Mock DNS resolver for predictable testing."""

    def __init__(self):
        self.responses = {
            "home.xn--wersdrfer-47a.de": {
                "192.168.178.94": ["192.168.178.94"],  # LAN response
                "100.119.21.93": ["100.119.21.93"],  # Tailscale response
                None: ["192.168.178.94"],  # Default response
            },
            "example.com": {None: ["93.184.216.34"]},
        }

    async def resolve(self, domain, rdtype, source=None):
        """Mock DNS resolution."""
        # Get the source IP from the parameter or resolver attribute
        source_ip = source or getattr(self, "source", None)

        # Get DNS server
        dns_server = None
        if hasattr(self, "nameservers") and self.nameservers:
            dns_server = self.nameservers[0]

        # Determine response based on domain and source/server
        domain_responses = self.responses.get(domain, {})

        # Try to match by DNS server first, then source IP, then default
        ips = (
            domain_responses.get(dns_server)
            or domain_responses.get(source_ip)
            or domain_responses.get(None, [])
        )

        # Create mock answer
        mock_answer = MagicMock()
        mock_answer.rrset = []
        for ip in ips:
            mock_rdata = MagicMock()
            mock_rdata.to_text.return_value = ip
            mock_answer.rrset.append(mock_rdata)

        mock_answer.__iter__ = MagicMock(return_value=iter(mock_answer.rrset))
        mock_answer.response = MagicMock()
        mock_answer.response.rcode = MagicMock(return_value=0)  # NOERROR
        mock_answer.qname = MagicMock()
        mock_answer.qname.to_text.return_value = f"{domain}."
        mock_answer.rdtype = dns.rdatatype.A

        return mock_answer


def test_dns_check_through_message_bus():
    """Test DNS check execution through the complete message bus flow."""
    # Bootstrap the system
    bus = bootstrap()
    uow = bus.uow

    # Create a DNS check
    check = Check(
        check_id=1,
        service_id=1,
        name="DNS LAN Check",
        check_type=CheckType.DNS,
        url="home.xn--wersdrfer-47a.de",
        check_interval=300,
        next_check_time=0,
        processing_started_at=0,
        status="idle",
        disabled=False,
        data={
            "expected_ips": ["192.168.178.94"],
            "dns_server": "192.168.178.94",
            "source_ip": "192.168.178.50",
        },
    )

    # Add check to repository
    uow.store.checks.add(check)
    assert len(uow.store.checks.list()) == 1

    # Mock DNS resolver
    with patch("dns.asyncresolver.Resolver") as mock_resolver_class:
        mock_resolver = MockDnsResolver()
        mock_resolver_class.return_value = mock_resolver

        # Execute checks through message bus
        bus.handle(ExecuteChecks(checks=[check]))

    # Verify result was stored
    results = uow.store.results.list()
    assert len(results) == 1
    result = results[0]

    # Verify result is successful
    assert result.status == ResultStatus.OK
    assert result.check_id == 1
    assert "resolved_ips" in result.data
    assert "192.168.178.94" in result.data["resolved_ips"]


def test_dns_check_resolution_mismatch():
    """Test DNS check with resolution mismatch."""
    bus = bootstrap()
    uow = bus.uow

    # Create a DNS check expecting wrong IP
    check = Check(
        check_id=2,
        service_id=1,
        name="DNS Mismatch Check",
        check_type=CheckType.DNS,
        url="example.com",
        check_interval=300,
        next_check_time=0,
        processing_started_at=0,
        status="idle",
        disabled=False,
        data={
            "expected_ips": ["192.168.1.1"],  # Wrong expected IP
            "dns_server": "8.8.8.8",
        },
    )

    uow.store.checks.add(check)

    with patch("dns.asyncresolver.Resolver") as mock_resolver_class:
        mock_resolver = MockDnsResolver()
        mock_resolver_class.return_value = mock_resolver

        bus.handle(ExecuteChecks(checks=[check]))

    # Verify error result
    results = uow.store.results.list()
    assert len(results) == 1
    result = results[0]

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "resolution_mismatch"
    assert result.data["expected"] == ["192.168.1.1"]
    assert "93.184.216.34" in result.data["actual"]


def test_dns_check_with_tailscale_source():
    """Test DNS check from Tailscale network perspective."""
    bus = bootstrap()
    uow = bus.uow

    # Create a DNS check from Tailscale perspective
    check = Check(
        check_id=3,
        service_id=1,
        name="DNS Tailscale Check",
        check_type=CheckType.DNS,
        url="home.xn--wersdrfer-47a.de",
        check_interval=300,
        next_check_time=0,
        processing_started_at=0,
        status="idle",
        disabled=False,
        data={
            "expected_ips": ["100.119.21.93"],
            "dns_server": "100.119.21.93",
            "source_ip": "100.119.21.50",  # Tailscale source IP
        },
    )

    uow.store.checks.add(check)

    with patch("dns.asyncresolver.Resolver") as mock_resolver_class:
        mock_resolver = MockDnsResolver()
        mock_resolver_class.return_value = mock_resolver

        bus.handle(ExecuteChecks(checks=[check]))

    # Verify result
    results = uow.store.results.list()
    assert len(results) == 1
    result = results[0]

    assert result.status == ResultStatus.OK
    assert "100.119.21.93" in result.data["resolved_ips"]


def test_dns_check_nxdomain():
    """Test DNS check for non-existent domain."""
    bus = bootstrap()
    uow = bus.uow

    check = Check(
        check_id=4,
        service_id=1,
        name="DNS NXDOMAIN Check",
        check_type=CheckType.DNS,
        url="nonexistent.invalid",
        check_interval=300,
        next_check_time=0,
        processing_started_at=0,
        status="idle",
        disabled=False,
        data={"expected_ips": ["192.168.1.1"]},
    )

    uow.store.checks.add(check)

    with patch("dns.asyncresolver.Resolver") as mock_resolver_class:
        mock_resolver = AsyncMock()
        mock_resolver_class.return_value = mock_resolver
        mock_resolver.resolve = AsyncMock(side_effect=dns.resolver.NXDOMAIN())

        bus.handle(ExecuteChecks(checks=[check]))

    # Verify error result
    results = uow.store.results.list()
    assert len(results) == 1
    result = results[0]

    assert result.status == ResultStatus.ERROR
    assert "nxdomain" in result.data["error_type"].lower()


def test_mixed_http_and_dns_checks():
    """Test that HTTP and DNS checks can coexist."""
    bus = bootstrap()
    uow = bus.uow

    # Create an HTTP check
    http_check = Check(
        check_id=10,
        service_id=1,
        name="HTTP Check",
        check_type=CheckType.HTTP,
        url="http://example.com",
        check_interval=300,
        next_check_time=0,
        processing_started_at=0,
        status="idle",
        disabled=False,
        data={},
    )

    # Create a DNS check
    dns_check = Check(
        check_id=11,
        service_id=1,
        name="DNS Check",
        check_type=CheckType.DNS,
        url="example.com",
        check_interval=300,
        next_check_time=0,
        processing_started_at=0,
        status="idle",
        disabled=False,
        data={"expected_ips": ["93.184.216.34"]},
    )

    uow.store.checks.add(http_check)
    uow.store.checks.add(dns_check)

    # Mock both HTTP and DNS
    with (
        patch("httpx.AsyncClient.get") as mock_http_get,
        patch("dns.asyncresolver.Resolver") as mock_resolver_class,
    ):
        # Mock HTTP response
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_http_get.return_value = mock_response

        # Mock DNS resolver
        mock_resolver = MockDnsResolver()
        mock_resolver_class.return_value = mock_resolver

        # Execute both checks
        bus.handle(ExecuteChecks(checks=[http_check, dns_check]))

    # Verify both results were stored
    results = uow.store.results.list()
    assert len(results) == 2

    # Find results by check_id
    http_result = next(r for r in results if r.check_id == 10)
    dns_result = next(r for r in results if r.check_id == 11)

    # Verify HTTP check succeeded
    assert http_result.status == ResultStatus.OK

    # Verify DNS check succeeded
    assert dns_result.status == ResultStatus.OK
    assert "93.184.216.34" in dns_result.data["resolved_ips"]


def test_dns_check_invalid_configuration():
    """Test DNS check with invalid configuration."""
    bus = bootstrap()
    uow = bus.uow

    # Create a DNS check with missing required field
    check = Check(
        check_id=5,
        service_id=1,
        name="Invalid DNS Check",
        check_type=CheckType.DNS,
        url="example.com",
        check_interval=300,
        next_check_time=0,
        processing_started_at=0,
        status="idle",
        disabled=False,
        data={
            # Missing expected_ips
            "dns_server": "8.8.8.8"
        },
    )

    uow.store.checks.add(check)
    bus.handle(ExecuteChecks(checks=[check]))

    # Verify error result
    results = uow.store.results.list()
    assert len(results) == 1
    result = results[0]

    assert result.status == ResultStatus.ERROR
    assert "expected_ips" in str(
        result.data.get("error_msg", "")
    ) or "configuration" in result.data.get("error_type", "")


def test_dns_check_updates_next_check_time():
    """Test that DNS check updates next_check_time after execution."""
    bus = bootstrap()
    uow = bus.uow

    check = Check(
        check_id=6,
        service_id=1,
        name="DNS Schedule Check",
        check_type=CheckType.DNS,
        url="example.com",
        check_interval=300,  # 5 minutes
        next_check_time=0,
        processing_started_at=0,
        status="idle",
        disabled=False,
        data={"expected_ips": ["93.184.216.34"]},
    )

    uow.store.checks.add(check)

    with patch("dns.asyncresolver.Resolver") as mock_resolver_class:
        mock_resolver = MockDnsResolver()
        mock_resolver_class.return_value = mock_resolver

        bus.handle(ExecuteChecks(checks=[check]))

    # Get updated check from repository
    updated_checks = uow.store.checks.list()
    updated_check = updated_checks[0]

    # Verify next_check_time was updated
    assert updated_check.next_check_time > 0
    assert updated_check.status == "idle"  # Status should be reset to idle
