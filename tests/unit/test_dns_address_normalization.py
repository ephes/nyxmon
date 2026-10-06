"""DNS expectations are compared by address value and family-checked."""

from __future__ import annotations

from typing import Any

import pytest

from nyxmon.adapters.runner.executors.dns_executor import (
    DnsCheckExecutor,
    DnsResolverResult,
)
from nyxmon.domain import Check, CheckType, ResultStatus
from nyxmon.domain.dns_config import DnsCheckConfig, normalize_dns_value


class _StubResolver:
    def __init__(self, records: list[str]) -> None:
        self.records = records

    async def query(self, domain: str, config: DnsCheckConfig) -> DnsResolverResult:
        del domain, config
        return DnsResolverResult(records=self.records, metadata={})


def _check(config: dict[str, Any]) -> Check:
    return Check(
        check_id=1,
        service_id=1,
        name="DNS",
        check_type=CheckType.DNS,
        url="example.com",
        data=config,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "stored",
    [
        "2A01:04F8:0000::0001",
        "2a01:4f8:0:0:0:0:0:1",
        "2a01:4f8::1",
        " 2a01:4f8::1 ",
    ],
)
async def test_legacy_raw_aaaa_expectation_matches(stored: str) -> None:
    """Rows saved before normalization still match without a migration."""
    executor = DnsCheckExecutor(resolver=_StubResolver(["2a01:4f8::1"]))

    result = await executor.execute(
        _check({"expected_ips": [stored], "query_type": "AAAA"})
    )

    assert result.status == ResultStatus.OK


@pytest.mark.anyio
async def test_different_address_still_mismatches() -> None:
    executor = DnsCheckExecutor(resolver=_StubResolver(["2a01:4f8::2"]))

    result = await executor.execute(
        _check({"expected_ips": ["2A01:4F8::1"], "query_type": "AAAA"})
    )

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "resolution_mismatch"


@pytest.mark.anyio
async def test_non_ip_values_keep_exact_matching() -> None:
    executor = DnsCheckExecutor(resolver=_StubResolver(["mail.example.com."]))

    ok = await executor.execute(
        _check({"expected_ips": ["mail.example.com."], "query_type": "MX"})
    )
    mismatch = await executor.execute(
        _check({"expected_ips": ["MAIL.example.com."], "query_type": "MX"})
    )

    assert ok.status == ResultStatus.OK
    assert mismatch.status == ResultStatus.ERROR


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("expected", "answer"),
    [("2A01:04F8:0000::0001", "2a01:4f8::1"), (" 192.0.2.1", "192.0.2.1")],
)
async def test_ip_looking_txt_values_are_compared_exactly(
    expected: str, answer: str
) -> None:
    executor = DnsCheckExecutor(resolver=_StubResolver([answer]))

    result = await executor.execute(
        _check({"expected_ips": [expected], "query_type": "TXT"})
    )

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "resolution_mismatch"


def test_normalize_dns_value() -> None:
    assert normalize_dns_value("2A01:04F8:0000::0001") == "2a01:4f8::1"
    assert normalize_dns_value("192.0.2.1") == "192.0.2.1"
    assert normalize_dns_value("not an ip") == "not an ip"


@pytest.mark.parametrize(
    ("query_type", "expected_ip"),
    [("AAAA", "192.0.2.1"), ("A", "2001:db8::1"), ("A", "not-an-ip")],
)
def test_validate_rejects_wrong_family(query_type: str, expected_ip: str) -> None:
    config = DnsCheckConfig(expected_ips=[expected_ip], query_type=query_type)

    with pytest.raises(ValueError, match="Invalid expected_ips entry"):
        config.validate()


@pytest.mark.anyio
async def test_wrong_family_is_a_configuration_error() -> None:
    executor = DnsCheckExecutor(resolver=_StubResolver(["2001:db8::1"]))

    result = await executor.execute(
        _check({"expected_ips": ["192.0.2.1"], "query_type": "AAAA"})
    )

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"


@pytest.mark.parametrize("expected_ips", ["192.0.2.1", ["192.0.2.1", 7], {"a": 1}])
def test_from_dict_requires_a_list_of_strings(expected_ips: Any) -> None:
    with pytest.raises(ValueError, match="must be a list of strings"):
        DnsCheckConfig.from_dict({"expected_ips": expected_ips})


@pytest.mark.anyio
async def test_string_expected_ips_is_a_configuration_error() -> None:
    executor = DnsCheckExecutor(resolver=_StubResolver(["1"]))

    result = await executor.execute(_check({"expected_ips": "192.0.2.1"}))

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
