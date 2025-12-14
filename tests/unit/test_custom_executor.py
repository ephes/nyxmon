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
                    {
                        "path": "$.pools.fast.health",
                        "op": "==",
                        "value": "ONLINE",
                        "severity": "critical",
                    }
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
                {
                    "path": "$.disks[0].ok",
                    "op": "==",
                    "value": True,
                    "severity": "critical",
                }
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


def test_invalid_check_entry_not_dict() -> None:
    """Non-dict entries in checks should produce a configuration_error."""
    executor = CustomExecutor()
    check = _build_check(
        data={
            "mode": "ssh-json",
            "target": "root@fractal.fritz.box",
            "command": "nyxmon-storage-metrics",
            "checks": ["not a dict", 123],
        }
    )
    result = anyio.run(executor.execute, check)
    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
    assert "errors" in result.data
    assert len(result.data["errors"]) == 2


def test_invalid_check_entry_missing_keys() -> None:
    """Check entries missing required keys should produce a configuration_error."""
    executor = CustomExecutor()
    check = _build_check(
        data={
            "mode": "ssh-json",
            "target": "root@fractal.fritz.box",
            "command": "nyxmon-storage-metrics",
            "checks": [
                {"path": "$.foo"},  # missing op, value, severity
            ],
        }
    )
    result = anyio.run(executor.execute, check)
    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
    assert "errors" in result.data
    assert "missing required keys" in result.data["errors"][0]


def test_invalid_check_entry_bad_operator() -> None:
    """Check entries with invalid operator should produce a configuration_error."""
    executor = CustomExecutor()
    check = _build_check(
        data={
            "mode": "ssh-json",
            "target": "root@fractal.fritz.box",
            "command": "nyxmon-storage-metrics",
            "checks": [
                {
                    "path": "$.foo",
                    "op": "invalid_op",
                    "value": 1,
                    "severity": "critical",
                },
            ],
        }
    )
    result = anyio.run(executor.execute, check)
    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
    assert "errors" in result.data
    assert "invalid operator" in result.data["errors"][0]


def test_check_data_not_dict() -> None:
    """check.data must be a dict; non-dict should produce a configuration_error."""
    from nyxmon.domain import Check

    executor = CustomExecutor()
    check = Check(
        check_id=1,
        service_id=1,
        name="Custom",
        check_type="custom",
        url="root@fractal.fritz.box",
        data="this is a string, not a dict",
    )
    result = anyio.run(executor.execute, check)
    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
    assert "must be a dict" in result.data["error_msg"]


def test_ssh_args_must_be_list_of_strings() -> None:
    """ssh_args must be a list of strings."""
    executor = CustomExecutor()
    check = _build_check(
        data={
            "mode": "ssh-json",
            "target": "root@fractal.fritz.box",
            "command": "nyxmon-storage-metrics",
            "ssh_args": ["-o", 123],  # 123 is not a string
            "checks": [
                {
                    "path": "$.foo",
                    "op": "==",
                    "value": "bar",
                    "severity": "critical",
                },
            ],
        }
    )
    result = anyio.run(executor.execute, check)
    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
    assert "ssh_args must be a list of strings" in result.data["error_msg"]


def test_ssh_args_not_list() -> None:
    """ssh_args must be a list, not a string."""
    executor = CustomExecutor()
    check = _build_check(
        data={
            "mode": "ssh-json",
            "target": "root@fractal.fritz.box",
            "command": "nyxmon-storage-metrics",
            "ssh_args": "-o BatchMode=yes",  # string, not a list
            "checks": [
                {
                    "path": "$.foo",
                    "op": "==",
                    "value": "bar",
                    "severity": "critical",
                },
            ],
        }
    )
    result = anyio.run(executor.execute, check)
    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
    assert "ssh_args must be a list of strings" in result.data["error_msg"]


def test_explicit_empty_ssh_args(monkeypatch) -> None:
    """Explicit empty ssh_args list should be allowed without falling back to defaults."""
    captured_args = {}

    def _stub_run(target, ssh_args, command, timeout):
        captured_args["ssh_args"] = ssh_args
        return '{"foo":"bar"}'

    monkeypatch.setattr(
        "nyxmon.adapters.runner.executors.custom_executor._run_ssh_json", _stub_run
    )

    executor = CustomExecutor()
    check = _build_check(
        data={
            "mode": "ssh-json",
            "target": "root@fractal.fritz.box",
            "command": "nyxmon-storage-metrics",
            "ssh_args": [],  # explicit empty list
            "checks": [
                {
                    "path": "$.foo",
                    "op": "==",
                    "value": "bar",
                    "severity": "critical",
                },
            ],
        }
    )
    result = anyio.run(executor.execute, check)
    assert result.status == ResultStatus.OK
    assert captured_args["ssh_args"] == []  # should be empty, not defaults


def test_ssh_argv_construction(monkeypatch) -> None:
    """Verify the constructed SSH argv has correct argument ordering.

    The -- separator must come BEFORE the target to properly separate
    ssh options from the destination (POSIX convention).
    """
    from nyxmon.adapters.runner.executors.custom_executor import _run_ssh_json

    captured_cmd = {}

    def _mock_subprocess_run(cmd, **kwargs):
        captured_cmd["argv"] = cmd

        # Create a mock result object
        class MockResult:
            stdout = '{"result": "ok"}'

        return MockResult()

    monkeypatch.setattr(subprocess, "run", _mock_subprocess_run)

    # Test with string command
    _run_ssh_json(
        target="user@host.example.com",
        ssh_args=["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"],
        command="/usr/local/bin/metrics",
        timeout=10.0,
    )

    assert captured_cmd["argv"] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "--",
        "user@host.example.com",
        "/usr/local/bin/metrics",
    ]


def test_ssh_argv_construction_with_list_command(monkeypatch) -> None:
    """Verify SSH argv construction when command is a list."""
    from nyxmon.adapters.runner.executors.custom_executor import _run_ssh_json

    captured_cmd = {}

    def _mock_subprocess_run(cmd, **kwargs):
        captured_cmd["argv"] = cmd

        class MockResult:
            stdout = '{"result": "ok"}'

        return MockResult()

    monkeypatch.setattr(subprocess, "run", _mock_subprocess_run)

    # Test with list command
    _run_ssh_json(
        target="root@server",
        ssh_args=["-p", "2222"],
        command=["python3", "-c", "print('hello')"],
        timeout=5.0,
    )

    assert captured_cmd["argv"] == [
        "ssh",
        "-p",
        "2222",
        "--",
        "root@server",
        "python3",
        "-c",
        "print('hello')",
    ]


def test_command_wrong_type() -> None:
    """command must be a string or list[str], not int or other types."""
    executor = CustomExecutor()
    check = _build_check(
        data={
            "mode": "ssh-json",
            "target": "root@fractal.fritz.box",
            "command": 123,  # wrong type
            "checks": [
                {
                    "path": "$.foo",
                    "op": "==",
                    "value": "bar",
                    "severity": "critical",
                },
            ],
        }
    )
    result = anyio.run(executor.execute, check)
    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
    assert "command must be a string or list of strings" in result.data["error_msg"]


def test_command_list_with_non_strings() -> None:
    """command list must contain only strings."""
    executor = CustomExecutor()
    check = _build_check(
        data={
            "mode": "ssh-json",
            "target": "root@fractal.fritz.box",
            "command": ["python3", 123],  # 123 is not a string
            "checks": [
                {
                    "path": "$.foo",
                    "op": "==",
                    "value": "bar",
                    "severity": "critical",
                },
            ],
        }
    )
    result = anyio.run(executor.execute, check)
    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
    assert "command list must contain only strings" in result.data["error_msg"]


def test_timeout_non_numeric() -> None:
    """timeout must be a number."""
    executor = CustomExecutor()
    check = _build_check(
        data={
            "mode": "ssh-json",
            "target": "root@fractal.fritz.box",
            "command": "metrics",
            "timeout": "not a number",
            "checks": [
                {
                    "path": "$.foo",
                    "op": "==",
                    "value": "bar",
                    "severity": "critical",
                },
            ],
        }
    )
    result = anyio.run(executor.execute, check)
    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
    assert "timeout must be a number" in result.data["error_msg"]


def test_retries_non_numeric() -> None:
    """retries must be an integer."""
    executor = CustomExecutor()
    check = _build_check(
        data={
            "mode": "ssh-json",
            "target": "root@fractal.fritz.box",
            "command": "metrics",
            "retries": "not a number",
            "checks": [
                {
                    "path": "$.foo",
                    "op": "==",
                    "value": "bar",
                    "severity": "critical",
                },
            ],
        }
    )
    result = anyio.run(executor.execute, check)
    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
    assert "retries must be an integer" in result.data["error_msg"]


def test_retry_delay_non_numeric() -> None:
    """retry_delay must be a number."""
    executor = CustomExecutor()
    check = _build_check(
        data={
            "mode": "ssh-json",
            "target": "root@fractal.fritz.box",
            "command": "metrics",
            "retry_delay": "not a number",
            "checks": [
                {
                    "path": "$.foo",
                    "op": "==",
                    "value": "bar",
                    "severity": "critical",
                },
            ],
        }
    )
    result = anyio.run(executor.execute, check)
    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
    assert "retry_delay must be a number" in result.data["error_msg"]
