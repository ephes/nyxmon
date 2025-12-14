"""Custom check executor.

Currently supports running a command over SSH and evaluating JSON thresholds.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any

import anyio

from ....domain import Check, Result, ResultStatus
from .json_metrics_executor import OPERATORS


class CustomExecutor:
    async def execute(self, check: Check) -> Result:
        if check.data is not None and not isinstance(check.data, dict):
            return Result(
                check_id=check.check_id,
                status=ResultStatus.ERROR,
                data={
                    "error_type": "configuration_error",
                    "error_msg": f"check.data must be a dict, got {type(check.data).__name__}",
                },
            )
        config = check.data or {}
        mode = config.get("mode", "ssh-json")
        if mode != "ssh-json":
            return Result(
                check_id=check.check_id,
                status=ResultStatus.ERROR,
                data={
                    "error_type": "configuration_error",
                    "error_msg": f"Unsupported custom mode: {mode}",
                },
            )

        target = config.get("target") or check.url
        if not target:
            return Result(
                check_id=check.check_id,
                status=ResultStatus.ERROR,
                data={
                    "error_type": "configuration_error",
                    "error_msg": "target is required",
                },
            )

        command = config.get("command")
        if not command:
            return Result(
                check_id=check.check_id,
                status=ResultStatus.ERROR,
                data={
                    "error_type": "configuration_error",
                    "error_msg": "command is required",
                },
            )

        checks = config.get("checks", [])
        if not checks:
            return Result(
                check_id=check.check_id,
                status=ResultStatus.ERROR,
                data={
                    "error_type": "configuration_error",
                    "error_msg": "checks must not be empty",
                },
            )

        # Validate each check entry is a dict with required keys
        validation_errors = _validate_checks(checks)
        if validation_errors:
            return Result(
                check_id=check.check_id,
                status=ResultStatus.ERROR,
                data={
                    "error_type": "configuration_error",
                    "error_msg": "invalid check definitions",
                    "errors": validation_errors,
                },
            )

        timeout = float(config.get("timeout", 15.0))
        retries = int(config.get("retries", 0))
        retry_delay = float(config.get("retry_delay", 2.0))

        ssh_args_default = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
        if "ssh_args" in config:
            ssh_args = config["ssh_args"]
            if not isinstance(ssh_args, list) or not all(
                isinstance(arg, str) for arg in ssh_args
            ):
                return Result(
                    check_id=check.check_id,
                    status=ResultStatus.ERROR,
                    data={
                        "error_type": "configuration_error",
                        "error_msg": "ssh_args must be a list of strings",
                    },
                )
        else:
            ssh_args = ssh_args_default

        attempts = retries + 1
        last_error: Result | None = None

        for attempt in range(attempts):
            start = time.time()
            try:
                stdout = await anyio.to_thread.run_sync(
                    _run_ssh_json,
                    target,
                    ssh_args,
                    command,
                    timeout,
                )
                payload = json.loads(stdout)
            except subprocess.TimeoutExpired as err:
                last_error = Result(
                    check_id=check.check_id,
                    status=ResultStatus.ERROR,
                    data={"error_type": "timeout", "error_msg": str(err)},
                )
                if attempt < retries:
                    await anyio.sleep(retry_delay)
                    continue
                return last_error
            except subprocess.CalledProcessError as err:
                last_error = Result(
                    check_id=check.check_id,
                    status=ResultStatus.ERROR,
                    data={
                        "error_type": "ssh_error",
                        "error_msg": (err.stderr or err.stdout or str(err)).strip(),
                    },
                )
                if attempt < retries:
                    await anyio.sleep(retry_delay)
                    continue
                return last_error
            except json.JSONDecodeError as err:
                return Result(
                    check_id=check.check_id,
                    status=ResultStatus.ERROR,
                    data={"error_type": "json_error", "error_msg": str(err)},
                )
            except Exception as err:  # noqa: BLE001
                return Result(
                    check_id=check.check_id,
                    status=ResultStatus.ERROR,
                    data={"error_type": "unexpected_error", "error_msg": str(err)},
                )

            duration_ms = int((time.time() - start) * 1000)
            failures = _evaluate(payload, checks)
            if failures:
                return Result(
                    check_id=check.check_id,
                    status=ResultStatus.ERROR,
                    data={
                        "error_type": "threshold_failed",
                        "failures": failures,
                        "duration_ms": duration_ms,
                    },
                )

            return Result(
                check_id=check.check_id,
                status=ResultStatus.OK,
                data={"duration_ms": duration_ms},
            )

        return last_error or Result(
            check_id=check.check_id,
            status=ResultStatus.ERROR,
            data={"error_type": "unexpected_error", "error_msg": "custom check failed"},
        )

    async def aclose(self) -> None:
        return None


def _run_ssh_json(
    target: str,
    ssh_args: list[str],
    command: str | list[str],
    timeout: float,
) -> str:
    # Place -- before target to separate ssh options from destination (POSIX convention)
    cmd = ["ssh", *ssh_args, "--", target]
    if isinstance(command, list):
        cmd.extend(command)
    else:
        cmd.append(command)
    proc = subprocess.run(
        cmd, check=True, capture_output=True, text=True, timeout=timeout
    )
    return proc.stdout.strip()


def _validate_checks(checks: list) -> list[str]:
    """Validate that each check entry is a properly-formed dict.

    Returns a list of error messages (empty if all valid).
    """
    errors: list[str] = []
    required_keys = {"path", "op", "value", "severity"}
    valid_ops = set(OPERATORS.keys())
    valid_severities = {"warning", "critical"}

    for idx, chk in enumerate(checks):
        if not isinstance(chk, dict):
            errors.append(f"checks[{idx}]: expected dict, got {type(chk).__name__}")
            continue

        missing = required_keys - set(chk.keys())
        if missing:
            errors.append(f"checks[{idx}]: missing required keys: {sorted(missing)}")
            continue

        op = chk.get("op")
        if op not in valid_ops:
            errors.append(
                f"checks[{idx}]: invalid operator '{op}', must be one of {sorted(valid_ops)}"
            )

        severity = chk.get("severity")
        if severity not in valid_severities:
            errors.append(
                f"checks[{idx}]: invalid severity '{severity}', must be 'warning' or 'critical'"
            )

    return errors


def _evaluate(payload: Any, checks: list[dict]) -> list[dict]:
    failures: list[dict] = []
    for chk in checks:
        path = chk.get("path", "")
        op = chk.get("op")
        expected = chk.get("value")
        severity = chk.get("severity")
        if not path or op not in OPERATORS or severity not in {"warning", "critical"}:
            failures.append(
                {
                    "path": path,
                    "op": op,
                    "expected": expected,
                    "actual": None,
                    "severity": severity or "critical",
                    "error": "invalid_check_definition",
                }
            )
            continue

        actual = _resolve_path(payload, path)
        comparator = OPERATORS[op]
        ok = False
        try:
            ok = comparator(actual, expected)
        except Exception:
            ok = False

        if not ok:
            failures.append(
                {
                    "path": path,
                    "op": op,
                    "expected": expected,
                    "actual": actual,
                    "severity": severity,
                }
            )

    return failures


def _resolve_path(payload: Any, path: str) -> Any:
    if path == "$":
        return payload

    normalized = re.sub(r"\[(\d+)\]", r".\1", path)
    parts = [p for p in normalized.replace("$.", "").split(".") if p]
    current = payload
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            idx = int(part)
            current = current[idx] if 0 <= idx < len(current) else None
        else:
            return None
    return current
