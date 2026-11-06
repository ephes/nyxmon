"""Exercise audit orchestration with synthetic exports and no network."""

import runpy
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/audit_python.py"


@pytest.mark.parametrize("failed_step,exit_code", [(None, 0), ("uv", 23), ("uvx", 1)])
def test_audit_propagates_failures_and_cleans_up(monkeypatch, failed_step, exit_code):
    commands = []
    requirements = None

    def run(command, *, check, stdout=None):
        nonlocal requirements
        commands.append(command)
        if command[0] == "uv":
            requirements = Path(stdout.name)
            stdout.write("dnspython==2.9.0\n")
        else:
            assert requirements.read_text() == "dnspython==2.9.0\n"
            assert command == [
                "uvx",
                "pip-audit==2.10.1",
                "--disable-pip",
                "--no-deps",
                "--cache-dir",
                str(requirements.parent / "cache"),
                "-r",
                str(requirements),
            ]
        if command[0] == failed_step:
            if check:
                raise subprocess.CalledProcessError(exit_code, command)
            return subprocess.CompletedProcess(command, exit_code)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    if exit_code:
        with pytest.raises(SystemExit) as error:
            runpy.run_path(str(SCRIPT), run_name="__main__")
        assert error.value.code == exit_code
    else:
        runpy.run_path(str(SCRIPT), run_name="__main__")

    assert commands[0] == [
        "uv",
        "export",
        "--frozen",
        "--all-extras",
        "--all-groups",
        "--no-emit-project",
        "--no-hashes",
    ]
    assert len(commands) == (1 if failed_step == "uv" else 2)
    assert requirements is not None
    assert not requirements.parent.exists()
