"""Additional tests for DNS source IP binding."""

from types import SimpleNamespace

import dns.rdatatype
import pytest

from nyxmon.adapters.runner.executors.dns_executor import (
    DnsCheckExecutor,
    DnspythonResolver,
)
from nyxmon.domain import Check, CheckType, ResultStatus


class RecordingAnswer:
    """Simple iterable answer object mirroring dnspython's interface."""

    def __init__(self, domain: str, rdtype: str) -> None:
        self.response = SimpleNamespace(rcode=lambda: 0)
        self.qname = SimpleNamespace(to_text=lambda: f"{domain}.")
        self.rdtype = getattr(dns.rdatatype, rdtype)
        self._records = [SimpleNamespace(to_text=lambda: "resolved")]

    def __iter__(self):
        return iter(self._records)


class RecordingResolver:
    """Test double that records resolve calls and returns canned answers."""

    def __init__(self) -> None:
        self.nameservers: list[str] = []
        self.timeout = 0
        self.lifetime = 0
        self.calls: list[dict] = []

    async def resolve(self, domain, rdtype, **kwargs):
        self.calls.append({"domain": domain, "rdtype": rdtype, **kwargs})
        rdtype_text = (
            rdtype if isinstance(rdtype, str) else dns.rdatatype.to_text(rdtype)
        )
        return RecordingAnswer(domain, rdtype_text)


def _make_check(**data):
    return Check(
        check_id=data.get("check_id", 1),
        service_id=1,
        name=data.get("name", "DNS Check"),
        check_type=CheckType.DNS,
        url=data.get("url", "example.com"),
        data=data.get("config", {"expected_ips": ["resolved"]}),
    )


class TestDnsSourceIpBinding:
    """Verify that `DnspythonResolver` handles source binding correctly."""

    @pytest.mark.anyio
    async def test_no_source_ip_specified(self, monkeypatch) -> None:
        recording_resolver = RecordingResolver()
        monkeypatch.setattr("dns.asyncresolver.Resolver", lambda: recording_resolver)

        executor = DnsCheckExecutor(resolver=DnspythonResolver())
        check = _make_check(config={"expected_ips": ["resolved"]})

        result = await executor.execute(check)

        assert result.status == ResultStatus.OK
        assert "source" not in recording_resolver.calls[0]
        assert "source_address" not in result.data

    @pytest.mark.anyio
    async def test_source_ip_is_passed_to_resolver(self, monkeypatch) -> None:
        recording_resolver = RecordingResolver()
        monkeypatch.setattr("dns.asyncresolver.Resolver", lambda: recording_resolver)

        executor = DnsCheckExecutor(resolver=DnspythonResolver())
        check = _make_check(
            config={
                "expected_ips": ["resolved"],
                "dns_server": "192.168.178.94",
                "source_ip": "192.168.178.50",
            }
        )

        result = await executor.execute(check)

        assert result.status == ResultStatus.OK
        assert recording_resolver.calls[0]["source"] == "192.168.178.50"
        assert result.data["source_address"] == "192.168.178.50"

    @pytest.mark.anyio
    async def test_query_type_is_respected(self, monkeypatch) -> None:
        recording_resolver = RecordingResolver()
        monkeypatch.setattr("dns.asyncresolver.Resolver", lambda: recording_resolver)

        executor = DnsCheckExecutor(resolver=DnspythonResolver())
        check = _make_check(
            config={
                "expected_ips": ["resolved"],
                "source_ip": "192.168.1.100",
                "query_type": "AAAA",
            }
        )

        result = await executor.execute(check)

        assert result.status == ResultStatus.OK
        assert recording_resolver.calls[0]["rdtype"] == "AAAA"
        assert result.data["source_address"] == "192.168.1.100"
