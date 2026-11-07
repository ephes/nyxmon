"""Exercise the local recipe with a portable fake npm shell and no network."""

import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_javascript_audit_includes_development_dependencies():
    script = json.loads((ROOT / "package.json").read_text())["scripts"]["audit"]
    assert shlex.split(script) == [
        "npm",
        "audit",
        "--include=dev",
        "--audit-level=moderate",
    ]


@pytest.mark.skipif(shutil.which("just") is None, reason="local recipe requires just")
@pytest.mark.parametrize("audit_exit", [0, 23])
def test_local_javascript_audit_flags_and_failure(tmp_path, monkeypatch, audit_exit):
    record = tmp_path / "commands.jsonl"
    fake = tmp_path / "fake_npm.py"
    fake.write_text(
        "import json, os, pathlib, shlex, subprocess, sys\n"
        "args = shlex.split(sys.argv[1])\n"
        "assert args.pop(0) == 'npm'\n"
        "with open(os.environ['AUDIT_RECORD'], 'a') as output:\n"
        "    output.write(json.dumps(args) + '\\n')\n"
        "if args == ['run', 'audit']:\n"
        "    command = json.loads(pathlib.Path('package.json').read_text())['scripts']['audit']\n"
        "    sys.exit(subprocess.call([sys.executable, __file__, command]))\n"
        "sys.exit(int(os.environ['AUDIT_EXIT']))\n"
    )
    monkeypatch.setenv("AUDIT_RECORD", str(record))
    monkeypatch.setenv("AUDIT_EXIT", str(audit_exit))
    # Replacing just's shell avoids executable shims and platform shell rules.
    result = subprocess.run(
        ["just", "--shell", sys.executable, "--shell-arg", str(fake), "js-audit"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert record.exists(), result.stderr
    assert [json.loads(line) for line in record.read_text().splitlines()] == [
        ["run", "audit"],
        ["audit", "--include=dev", "--audit-level=moderate"],
    ]
    assert (result.returncode == 0) == (audit_exit == 0), result.stderr
