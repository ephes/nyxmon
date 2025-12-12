"""Unit tests for the custom executor."""

import anyio
import subprocess

from nyxmon.adapters.runner.executors.custom_executor import CustomExecutor
from nyxmon.domain import Check, ResultStatus


def _build_check(**overrides):
    return Check(
        check_id=overrides.get("check_id", 1),
        service_id=1,
        name=overrides.get("name", "Custom"),
        check_type="custom",
        url=overrides.get("url", "root@fractal.fritz.box"),
        data=overrides.get(
            "data",
            {
                "mode": "ssh-json",
                "target": "root@fractal.fritz.box",
                "command": "nyxmon-storage-metrics",
                "timeout": 1,
                "retries": 0,
                "checks": [
                    {"path": "$.pools.fast.health", "op": "==", "value": "ONLINE", "severity": "critical"}
                ],
            },
        ),
    )


def test_success_returns_ok(monkeypatch) -> None:
    def _stub_run(*_args, **_kwargs):
        return '{"pools":{"fast":{"health":"ONLINE"}}}'

    monkeypatch.setattr(
        "nyxmon.adapters.runner.executors.custom_executor._run_ssh_json", _stub_run
    )

    executor = CustomExecutor()
    check = _build_check()
    result = anyio.run(executor.execute, check)

    assert result.status == ResultStatus.OK


def test_bracket_path_syntax(monkeypatch) -> None:
    def _stub_run(*_args, **_kwargs):
        return '{"disks":[{"ok":true}]}'

    monkeypatch.setattr(
        "nyxmon.adapters.runner.executors.custom_executor._run_ssh_json", _stub_run
    )

    executor = CustomExecutor()
    check = _build_check(
        data={
            "mode": "ssh-json",
            "target": "root@fractal.fritz.box",
            "command": "nyxmon-storage-metrics",
            "checks": [
                {"path": "$.disks[0].ok", "op": "==", "value": True, "severity": "critical"}
            ],
        }
    )
    result = anyio.run(executor.execute, check)
    assert result.status == ResultStatus.OK


def test_threshold_failure_returns_error(monkeypatch) -> None:
    def _stub_run(*_args, **_kwargs):
        return '{"pools":{"fast":{"health":"DEGRADED"}}}'

    monkeypatch.setattr(
        "nyxmon.adapters.runner.executors.custom_executor._run_ssh_json", _stub_run
    )

    executor = CustomExecutor()
    check = _build_check()
    result = anyio.run(executor.execute, check)

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "threshold_failed"


def test_ssh_error_returns_error(monkeypatch) -> None:
    def _stub_run(*_args, **_kwargs):
        raise subprocess.CalledProcessError(255, ["ssh"], stderr="no route")

    monkeypatch.setattr(
        "nyxmon.adapters.runner.executors.custom_executor._run_ssh_json", _stub_run
    )

    executor = CustomExecutor()
    check = _build_check()
    result = anyio.run(executor.execute, check)

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "ssh_error"


def test_configuration_error(monkeypatch) -> None:
    executor = CustomExecutor()
    check = _build_check(data={"mode": "ssh-json"})
    result = anyio.run(executor.execute, check)
    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
