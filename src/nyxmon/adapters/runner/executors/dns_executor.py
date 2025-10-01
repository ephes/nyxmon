"""DNS check executor implementation."""

import time
from dataclasses import dataclass
from typing import Any, List, Protocol

import dns.asyncresolver
import dns.rdatatype
import dns.rcode
import dns.resolver

from ....domain import Check, Result, ResultStatus
from ....domain.dns_config import DnsCheckConfig


@dataclass
class DnsResolverResult:
    """Structured result returned by DNS resolvers."""

    records: List[str]
    metadata: dict[str, Any]


class DnsResolver(Protocol):
    """Protocol describing the resolver interface expected by the executor."""

    async def query(self, domain: str, config: DnsCheckConfig) -> DnsResolverResult:
        """Resolve ``domain`` according to ``config`` and return structured data."""


class DnspythonResolver:
    """Resolver implementation backed by dnspython."""

    async def query(self, domain: str, config: DnsCheckConfig) -> DnsResolverResult:
        resolver = dns.asyncresolver.Resolver()

        if config.dns_server:
            resolver.nameservers = [config.dns_server]

        resolver.timeout = config.timeout
        resolver.lifetime = config.timeout

        if config.source_ip:
            answer = await resolver.resolve(
                domain, config.query_type, source=config.source_ip
            )
        else:
            answer = await resolver.resolve(domain, config.query_type)

        resolved_data: list[str] = []
        for rdata in answer:
            if config.query_type in {"A", "AAAA"}:
                resolved_data.append(rdata.to_text())
            elif config.query_type == "MX":
                resolved_data.append(rdata.exchange.to_text())
            elif config.query_type == "TXT":
                resolved_data.append(
                    " ".join(
                        [
                            s.decode() if isinstance(s, bytes) else s
                            for s in rdata.strings
                        ]
                    )
                )
            else:
                resolved_data.append(rdata.to_text())

        metadata: dict[str, Any] = {
            "response_code": dns.rcode.to_text(answer.response.rcode()),
            "questions": [
                f"{answer.qname.to_text()} IN {dns.rdatatype.to_text(answer.rdtype)}"
            ],
            "rrset": resolved_data.copy(),
            "dns_server": config.dns_server or "system",
        }

        if config.source_ip:
            metadata["source_address"] = config.source_ip

        return DnsResolverResult(records=resolved_data, metadata=metadata)


class DnsCheckExecutor:
    """Executor for DNS checks.

    Performs DNS queries and validates results against expected IPs.
    """

    def __init__(self, resolver: DnsResolver | None = None) -> None:
        self._resolver: DnsResolver = resolver or DnspythonResolver()

    async def execute(self, check: Check) -> Result:
        """Execute a DNS check and return a Result."""
        start_time = time.time()

        try:
            config = DnsCheckConfig.from_dict(check.data)
            config.validate()

            resolver_result = await self._resolver.query(check.url, config)
            resolved_ips = resolver_result.records
            dns_metadata = resolver_result.metadata

            query_time_ms = int((time.time() - start_time) * 1000)

            if self._check_ip_match(resolved_ips, config.expected_ips):
                return self._create_success_result(
                    check.check_id,
                    resolved_ips,
                    query_time_ms,
                    dns_metadata,
                )

            return self._create_mismatch_result(
                check.check_id,
                config.expected_ips,
                resolved_ips,
                query_time_ms,
                dns_metadata,
            )

        except dns.resolver.NXDOMAIN:
            return self._create_error_result(
                check.check_id,
                "nxdomain",
                f"Domain {check.url} does not exist",
            )
        except dns.resolver.Timeout:
            return self._create_error_result(
                check.check_id,
                "timeout",
                f"DNS query timed out for {check.url}",
            )
        except dns.resolver.NoAnswer:
            return self._create_error_result(
                check.check_id,
                "no_answer",
                f"No answer received for {check.url}",
            )
        except ValueError as err:
            return self._create_error_result(
                check.check_id,
                "configuration_error",
                str(err),
            )
        except Exception as err:  # noqa: BLE001 - bubble unexpected errors to result
            return self._create_error_result(
                check.check_id,
                "unexpected_error",
                str(err),
            )

    def _check_ip_match(self, resolved: List[str], expected: List[str]) -> bool:
        """Check if any resolved IP matches any expected IP.

        Args:
            resolved: List of resolved IPs
            expected: List of expected IPs

        Returns:
            True if there's at least one match
        """
        resolved_set = set(resolved)
        expected_set = set(expected)
        return bool(resolved_set & expected_set)

    def _create_success_result(
        self,
        check_id: int,
        resolved_ips: List[str],
        query_time_ms: int,
        metadata: dict[str, Any],
    ) -> Result:
        """Create a successful result.

        Args:
            check_id: The check ID
            resolved_ips: List of resolved IPs
            query_time_ms: Query time in milliseconds
            metadata: DNS metadata

        Returns:
            Success Result
        """
        data = {
            "resolved_ips": resolved_ips,
            "query_time_ms": query_time_ms,
            **metadata,
        }

        return Result(check_id=check_id, status=ResultStatus.OK, data=data)

    def _create_mismatch_result(
        self,
        check_id: int,
        expected: List[str],
        actual: List[str],
        query_time_ms: int,
        metadata: dict[str, Any],
    ) -> Result:
        """Create a resolution mismatch result.

        Args:
            check_id: The check ID
            expected: Expected IPs
            actual: Actual resolved IPs
            query_time_ms: Query time in milliseconds
            metadata: DNS metadata

        Returns:
            Error Result for mismatch
        """
        data = {
            "error_type": "resolution_mismatch",
            "expected": expected,
            "actual": actual,
            "query_time_ms": query_time_ms,
            **metadata,
        }

        return Result(check_id=check_id, status=ResultStatus.ERROR, data=data)

    def _create_error_result(
        self, check_id: int, error_type: str, error_msg: str
    ) -> Result:
        """Create an error result.

        Args:
            check_id: The check ID
            error_type: Type of error
            error_msg: Error message

        Returns:
            Error Result
        """
        return Result(
            check_id=check_id,
            status=ResultStatus.ERROR,
            data={"error_type": error_type, "error_msg": error_msg},
        )

    async def aclose(self) -> None:
        """Close the executor.

        DNS executor doesn't manage any resources, so this is a no-op.
        """
        pass
