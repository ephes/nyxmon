"""Unit tests for JsonMetricsCheckConfig."""

import pytest

from nyxmon.domain.json_metrics_config import JsonMetricsCheckConfig


class TestJsonMetricsCheckConfig:
    """Tests for JsonMetricsCheckConfig domain model."""

    def test_from_dict_with_required_fields(self) -> None:
        data = {
            "url": "http://localhost:9100/.well-known/health",
            "checks": [
                {
                    "path": "$.mail.queue_total",
                    "op": "<",
                    "value": 100,
                    "severity": "warning",
                }
            ],
        }

        config = JsonMetricsCheckConfig.from_dict(data)

        assert config.url == "http://localhost:9100/.well-known/health"
        assert config.timeout == 10.0
        assert config.auth is None
        assert len(config.checks) == 1
        assert config.retries == 1
        assert config.retry_delay == 2.0

    def test_from_dict_with_auth_and_overrides(self) -> None:
        data = {
            "url": "http://example/health",
            "timeout": 5.0,
            "auth": {"username": "u", "password": "p"},
            "checks": [
                {
                    "path": "$.disk.root_percent",
                    "op": "<",
                    "value": 80,
                    "severity": "critical",
                }
            ],
            "retries": 3,
            "retry_delay": 1.5,
        }

        config = JsonMetricsCheckConfig.from_dict(data)

        assert config.timeout == 5.0
        assert config.auth == {"username": "u", "password": "p"}
        assert config.checks[0].severity == "critical"
        assert config.retries == 3
        assert config.retry_delay == 1.5

    def test_to_dict_roundtrip(self) -> None:
        config = JsonMetricsCheckConfig(
            url="http://localhost:9100/.well-known/health",
            timeout=7.5,
            auth={"username": "nyx", "password": "mon"},
            checks=[
                JsonMetricsCheckConfig.Check(
                    path="$.services.postfix",
                    op="==",
                    value="active",
                    severity="critical",
                )
            ],
            retries=0,
            retry_delay=0.5,
        )

        data = config.to_dict()
        restored = JsonMetricsCheckConfig.from_dict(data)

        assert restored.url == config.url
        assert restored.timeout == 7.5
        assert restored.auth == {"username": "nyx", "password": "mon"}
        assert restored.checks[0].op == "=="
        assert restored.retries == 0
        assert restored.retry_delay == 0.5

    def test_missing_required_fields(self) -> None:
        with pytest.raises(ValueError):
            JsonMetricsCheckConfig.from_dict({})

        with pytest.raises(ValueError):
            JsonMetricsCheckConfig.from_dict({"url": "http://h", "checks": []})

    @pytest.mark.parametrize("bad_op", ["<>", "contains", ""])
    def test_invalid_operator(self, bad_op: str) -> None:
        with pytest.raises(ValueError, match="op"):
            JsonMetricsCheckConfig.from_dict(
                {
                    "url": "http://h",
                    "checks": [
                        {
                            "path": "$.a",
                            "op": bad_op,
                            "value": 1,
                            "severity": "critical",
                        }
                    ],
                }
            )

    @pytest.mark.parametrize("bad_severity", ["info", "high", ""])
    def test_invalid_severity(self, bad_severity: str) -> None:
        with pytest.raises(ValueError, match="severity"):
            JsonMetricsCheckConfig.from_dict(
                {
                    "url": "http://h",
                    "checks": [
                        {"path": "$.a", "op": "<", "value": 1, "severity": bad_severity}
                    ],
                }
            )

    def test_validate_numeric_limits(self) -> None:
        config = JsonMetricsCheckConfig(
            url="http://h",
            checks=[
                JsonMetricsCheckConfig.Check(
                    path="$.a", op="<", value=1, severity="warning"
                )
            ],
            retries=-1,
            retry_delay=-1,
            timeout=0,
        )

        with pytest.raises(ValueError):
            config.validate()
