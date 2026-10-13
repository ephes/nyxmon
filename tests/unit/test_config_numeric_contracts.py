"""Configuration admission rejects unsupported values before transport use."""

from unittest.mock import AsyncMock

import pytest

from nyxmon.adapters.runner.executors.dns_executor import DnsCheckExecutor
from nyxmon.adapters.runner.executors.http_executor import HttpCheckExecutor
from nyxmon.adapters.runner.executors.tcp_executor import TcpCheckExecutor
from nyxmon.domain import Check, CheckType, ResultStatus
from nyxmon.domain.dns_config import DnsCheckConfig
from nyxmon.domain.http_config import HttpCheckConfig
from nyxmon.domain.tcp_config import TcpCheckConfig


CONFIGS = [
    (
        TcpCheckConfig,
        {"port": 443},
        ["connect_timeout", "tls_handshake_timeout", "retry_delay"],
    ),
    (HttpCheckConfig, {}, ["timeout", "retry_delay"]),
    (DnsCheckConfig, {"expected_ips": ["192.0.2.1"]}, ["timeout"]),
]
DURATION_CASES = [(cls, base, name) for cls, base, names in CONFIGS for name in names]
INTEGER_CASES = [
    (TcpCheckConfig, {"port": 443}, "port", 1, 65535),
    (TcpCheckConfig, {"port": 443}, "retries", 0, 100),
    (TcpCheckConfig, {"port": 443}, "min_cert_days", 0, 36500),
    (HttpCheckConfig, {}, "retries", 0, 100),
    (HttpCheckConfig, {}, "expected_status", 100, 599),
    (HttpCheckConfig, {}, "retry_status_codes", 100, 599),
]


@pytest.mark.parametrize("admission", ["from_dict", "validate"])
@pytest.mark.parametrize("cls,base,name", DURATION_CASES)
@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        -float("inf"),
        True,
        False,
        "5",
        "junk",
        None,
        [],
        -1,
        301,
        10**400,
    ],
)
def test_invalid_duration(cls, base, name, value, admission):
    data = {**base, name: value}
    with pytest.raises(ValueError, match=name):
        if admission == "from_dict":
            cls.from_dict(data)
        else:
            cls(**data).validate()


@pytest.mark.parametrize("cls,base,name", DURATION_CASES)
@pytest.mark.parametrize("value", [0.25, 1, 300])
def test_supported_duration_roundtrip(cls, base, name, value):
    config = cls.from_dict({**base, name: value})
    assert config.validate()
    assert getattr(config, name) == value
    assert cls.from_dict(config.to_dict()) == config


@pytest.mark.parametrize("cls,base,name", DURATION_CASES)
def test_zero_duration_only_allowed_for_delay(cls, base, name):
    if name == "retry_delay":
        assert cls.from_dict({**base, name: 0}).validate()
    else:
        with pytest.raises(ValueError, match=name):
            cls.from_dict({**base, name: 0})
        with pytest.raises(ValueError, match=name):
            cls(**{**base, name: 0}).validate()


@pytest.mark.parametrize("admission", ["from_dict", "validate"])
@pytest.mark.parametrize("cls,base,name,minimum,maximum", INTEGER_CASES)
@pytest.mark.parametrize(
    "value",
    [True, False, 1.9, 200.0, "200", None, [], float("nan"), float("inf"), -1, 10**400],
)
def test_invalid_integer(cls, base, name, minimum, maximum, value, admission):
    # expected_status=None is the documented optional sentinel.
    if name == "expected_status" and value is None:
        return
    data = {**base, name: [value] if name == "retry_status_codes" else value}
    with pytest.raises(ValueError, match=name):
        if admission == "from_dict":
            cls.from_dict(data)
        else:
            cls(**data).validate()


@pytest.mark.parametrize("cls,base,name,minimum,maximum", INTEGER_CASES)
def test_integer_bounds(cls, base, name, minimum, maximum):
    for value in (minimum, maximum):
        data = {**base, name: [value] if name == "retry_status_codes" else value}
        assert cls.from_dict(data).validate()
    for value in (minimum - 1, maximum + 1):
        data = {**base, name: [value] if name == "retry_status_codes" else value}
        with pytest.raises(ValueError, match=name):
            cls.from_dict(data)
        with pytest.raises(ValueError, match=name):
            cls(**data).validate()


@pytest.mark.parametrize("admission", ["from_dict", "validate"])
@pytest.mark.parametrize(
    "name", ["verify", "check_cert_expiry", "starttls_read_greeting"]
)
@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, []])
def test_tcp_boolean_fields(name, value, admission):
    data = {"port": 443, name: value}
    with pytest.raises(ValueError, match=name):
        if admission == "from_dict":
            TcpCheckConfig.from_dict(data)
        else:
            TcpCheckConfig(**data).validate()


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), True, "5", "junk", None, 10**400]
)
@pytest.mark.parametrize("kind", [CheckType.TCP, CheckType.HTTP, CheckType.DNS])
@pytest.mark.anyio
async def test_invalid_budget_returns_configuration_error_before_transport(
    kind, value, monkeypatch
):
    transport = AsyncMock(
        side_effect=AssertionError("invalid configuration reached transport")
    )
    if kind == CheckType.TCP:
        executor = TcpCheckExecutor()
        monkeypatch.setattr(executor, "_attempt_once", transport)
        data = {"port": 443, "connect_timeout": value}
    elif kind == CheckType.HTTP:
        executor = HttpCheckExecutor()
        monkeypatch.setattr(executor, "_get_client", transport)
        data = {"timeout": value}
    else:
        resolver = AsyncMock()
        resolver.query = transport
        executor = DnsCheckExecutor(resolver=resolver)
        data = {"expected_ips": ["192.0.2.1"], "timeout": value}
    check = Check(
        check_id=1,
        service_id=1,
        name="contract",
        check_type=kind,
        url="example.test",
        data=data,
    )
    result = await executor.execute(check)
    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
    transport.assert_not_called()
