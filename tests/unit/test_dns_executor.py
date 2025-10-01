"""Unit tests for the DNS check executor."""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import dns.rdatatype
import dns.resolver
import pytest

from nyxmon.adapters.runner.executors.dns_executor import (
    DnsCheckExecutor,
    DnsResolverResult,
    DnspythonResolver,
)
from nyxmon.domain import Check, CheckType, ResultStatus


def _build_check(**data: Any) -> Check:
    return Check(
        check_id=data.get("check_id", 1),
        service_id=1,
        name=data.get("name", "DNS Test"),
        check_type=CheckType.DNS,
        url=data.get("url", "example.com"),
        data=data.get("config", {"expected_ips": ["127.0.0.1"]}),
    )


@dataclass
class StubResolver:
    """Test double implementing the resolver protocol."""

    result: DnsResolverResult | None = None
    exc: Exception | None = None
    delay: float = 0.0

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def query(self, domain: str, config):  # type: ignore[override]
        self.calls.append((domain, config))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        assert self.result is not None, "StubResolver.result must be provided"
        return self.result


class TestDnsCheckExecutor:
    """Behavioural tests using a stub resolver."""

    @pytest.mark.anyio
    async def test_successful_dns_resolution(self) -> None:
        resolver = StubResolver(
            result=DnsResolverResult(
                records=["192.168.178.94"],
                metadata={
                    "dns_server": "192.168.178.94",
                    "response_code": "NOERROR",
                    "questions": ["example.com. IN A"],
                    "rrset": ["192.168.178.94"],
                },
            )
        )
        executor = DnsCheckExecutor(resolver=resolver)

        check = _build_check(
            url="home.xn--wersdrfer-47a.de",
            config={
                "expected_ips": ["192.168.178.94"],
                "dns_server": "192.168.178.94",
            },
        )

        result = await executor.execute(check)

        assert resolver.calls, "Resolver should be invoked"
        assert result.status == ResultStatus.OK
        assert result.data["resolved_ips"] == ["192.168.178.94"]
        assert result.data["dns_server"] == "192.168.178.94"

    @pytest.mark.anyio
    async def test_resolution_mismatch(self) -> None:
        resolver = StubResolver(
            result=DnsResolverResult(
                records=["192.168.178.95"],
                metadata={
                    "dns_server": "system",
                    "response_code": "NOERROR",
                    "questions": [],
                    "rrset": ["192.168.178.95"],
                },
            )
        )
        executor = DnsCheckExecutor(resolver=resolver)

        check = _build_check(
            config={"expected_ips": ["192.168.178.94"], "dns_server": "192.168.178.94"},
        )

        result = await executor.execute(check)

        assert result.status == ResultStatus.ERROR
        assert result.data["error_type"] == "resolution_mismatch"
        assert result.data["expected"] == ["192.168.178.94"]
        assert result.data["actual"] == ["192.168.178.95"]

    @pytest.mark.anyio
    async def test_dns_timeout(self) -> None:
        resolver = StubResolver(exc=dns.resolver.Timeout())
        executor = DnsCheckExecutor(resolver=resolver)

        check = _build_check(config={"expected_ips": ["192.168.1.1"], "timeout": 0.1})

        result = await executor.execute(check)

        assert result.status == ResultStatus.ERROR
        assert result.data["error_type"] == "timeout"

    @pytest.mark.anyio
    async def test_nxdomain(self) -> None:
        resolver = StubResolver(exc=dns.resolver.NXDOMAIN())
        executor = DnsCheckExecutor(resolver=resolver)

        check = _build_check(config={"expected_ips": ["192.168.1.1"]})

        result = await executor.execute(check)

        assert result.status == ResultStatus.ERROR
        assert result.data["error_type"] == "nxdomain"

    @pytest.mark.anyio
    async def test_invalid_configuration(self) -> None:
        resolver = StubResolver(result=DnsResolverResult(records=[], metadata={}))
        executor = DnsCheckExecutor(resolver=resolver)

        check = _build_check(config={"dns_server": "8.8.8.8"})  # missing expected_ips

        result = await executor.execute(check)

        assert result.status == ResultStatus.ERROR
        assert "configuration" in result.data["error_type"]
        assert resolver.calls == []

    @pytest.mark.anyio
    async def test_query_time_measurement(self) -> None:
        resolver = StubResolver(
            result=DnsResolverResult(
                records=["127.0.0.1"],
                metadata={
                    "dns_server": "system",
                    "response_code": "NOERROR",
                    "questions": [],
                    "rrset": ["127.0.0.1"],
                },
            ),
            delay=0.05,
        )
        executor = DnsCheckExecutor(resolver=resolver)

        check = _build_check(config={"expected_ips": ["127.0.0.1"]})

        result = await executor.execute(check)

        assert result.status == ResultStatus.OK
        assert result.data["query_time_ms"] >= 50

    @pytest.mark.anyio
    async def test_metadata_passthrough(self) -> None:
        metadata = {
            "dns_server": "system",
            "response_code": "NOERROR",
            "questions": ["example.com. IN MX"],
            "rrset": ["mail.example.com."],
            "source_address": "192.168.178.50",
        }
        resolver = StubResolver(
            result=DnsResolverResult(records=["mail.example.com."], metadata=metadata)
        )
        executor = DnsCheckExecutor(resolver=resolver)

        check = _build_check(
            config={"expected_ips": ["mail.example.com."], "query_type": "MX"}
        )

        result = await executor.execute(check)

        assert result.status == ResultStatus.OK
        for key, value in metadata.items():
            assert result.data[key] == value


class TestDnspythonResolver:
    """Focused tests for the dnspython-backed resolver."""

    @pytest.mark.anyio
    async def test_resolver_binds_source_ip(self, monkeypatch) -> None:
        captured_kwargs: dict[str, Any] = {}

        class FakeAnswer:
            def __init__(self, domain: str) -> None:
                self._domain = domain
                self.response = SimpleNamespace(rcode=lambda: 0)
                self.qname = SimpleNamespace(to_text=lambda: f"{domain}.")
                self.rdtype = dns.rdatatype.A
                self._records = [SimpleNamespace(to_text=lambda: "192.168.178.94")]

            def __iter__(self):
                return iter(self._records)

        class FakeResolver:
            def __init__(self):
                self.nameservers: list[str] = []
                self.timeout = 0
                self.lifetime = 0

            async def resolve(self, domain, rdtype, **kwargs):
                captured_kwargs.update({"domain": domain, "rdtype": rdtype, **kwargs})
                return FakeAnswer(domain)

        monkeypatch.setattr("dns.asyncresolver.Resolver", FakeResolver)

        executor = DnsCheckExecutor(resolver=DnspythonResolver())
        check = _build_check(
            config={
                "expected_ips": ["192.168.178.94"],
                "source_ip": "192.168.178.50",
            }
        )

        result = await executor.execute(check)

        assert result.status == ResultStatus.OK
        assert captured_kwargs["source"] == "192.168.178.50"
