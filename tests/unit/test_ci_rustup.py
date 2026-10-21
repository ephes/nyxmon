"""Hosted-runner admission, artifact verification and later-shell dispatch isolation."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ci_rustup.py"
spec = importlib.util.spec_from_file_location("ci_rustup", SCRIPT)
assert spec and spec.loader
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


@pytest.fixture(autouse=True)
def package_refresh_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """These original stage tests isolate APT; APT caller tests live separately."""
    monkeypatch.setattr(bootstrap, "refresh_runner_ca", lambda env: None)


@pytest.fixture
def hosted(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env_file = tmp_path / "github-env"
    env_file.touch()
    return {
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": "Linux",
        "HOME": str(home),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_ENV": str(env_file),
    }


def test_refuses_local_or_self_hosted_before_any_io(hosted):
    for key, value in [
        ("GITHUB_ACTIONS", "false"),
        ("RUNNER_ENVIRONMENT", "self-hosted"),
        ("RUNNER_OS", "macOS"),
    ]:
        with (
            patch.object(bootstrap.platform, "system", return_value="Linux"),
            patch.object(bootstrap.platform, "machine", return_value="x86_64"),
            patch.object(bootstrap, "admit_ca") as admission,
        ):
            with pytest.raises(RuntimeError, match="disposable"):
                bootstrap.bootstrap({**hosted, key: value})
            admission.assert_not_called()


def test_requires_native_linux_arch_and_no_existing_bash_env(hosted):
    with (
        patch.object(bootstrap.platform, "system", return_value="Linux"),
        patch.object(bootstrap.platform, "machine", return_value="aarch64"),
    ):
        with pytest.raises(RuntimeError, match="disposable"):
            bootstrap.runner_paths(hosted)
    with (
        patch.object(bootstrap.platform, "system", return_value="Linux"),
        patch.object(bootstrap.platform, "machine", return_value="x86_64"),
    ):
        with pytest.raises(RuntimeError, match="BASH_ENV"):
            bootstrap.runner_paths({**hosted, "BASH_ENV": "keep-existing-config"})


def test_ca_admission_rejects_incident_root_and_empty_bundle(tmp_path):
    bundle = tmp_path / "ca"
    bundle.write_text("")
    with pytest.raises(RuntimeError, match="no certificates"):
        bootstrap.admit_ca(bundle)
    bundle.write_text(
        "-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----"
    )
    with patch.object(bootstrap, "BLOCKED", {hashlib.sha256(b"fake").hexdigest()}):
        with pytest.raises(RuntimeError, match="retains incident"):
            bootstrap.admit_ca(bundle)


def test_old_os_bundle_fails_before_download(hosted):
    with (
        patch.object(
            bootstrap,
            "runner_paths",
            return_value=tuple(
                Path(hosted[x]) for x in ("HOME", "RUNNER_TEMP", "GITHUB_ENV")
            ),
        ),
        patch.object(bootstrap, "admit_ca", side_effect=RuntimeError("old bundle")),
        patch.object(bootstrap.subprocess, "run") as run,
    ):
        with pytest.raises(RuntimeError, match="old bundle"):
            bootstrap.bootstrap(hosted)
        run.assert_not_called()
        assert Path(hosted["GITHUB_ENV"]).read_text() == ""


def test_checksum_mismatch_never_executes_candidate_or_publishes(hosted):
    def download(args, **kwargs):
        assert args[0] == "/usr/bin/curl"
        assert args[1] == "-q"
        assert args[args.index("--cacert") + 1] == str(bootstrap.CA_FILE)
        assert "--insecure" not in args and "-k" not in args
        Path(args[args.index("--output") + 1]).write_bytes(b"wrong artifact")
        return subprocess.CompletedProcess(args, 0)

    with (
        patch.object(
            bootstrap,
            "runner_paths",
            return_value=tuple(
                Path(hosted[x]) for x in ("HOME", "RUNNER_TEMP", "GITHUB_ENV")
            ),
        ),
        patch.object(bootstrap, "admit_ca", return_value=120),
        patch.object(bootstrap.subprocess, "run", side_effect=download) as run,
    ):
        with pytest.raises(RuntimeError, match="artifact checksum"):
            bootstrap.bootstrap(hosted)
        assert run.call_count == 1
        assert Path(hosted["GITHUB_ENV"]).read_text() == ""


def test_offline_replacement_and_installed_checksum_gate(hosted):
    artifact = b"verified fixture"
    calls = []

    def stage(args, **kwargs):
        calls.append(args)
        assert kwargs["env"].get("RUSTUP_FORCE_ARG0") is None
        if "--output" in args:
            Path(args[args.index("--output") + 1]).write_bytes(artifact)
        elif "--self-replace" in args:
            installed = Path(hosted["HOME"]) / ".cargo/bin/rustup"
            installed.parent.mkdir(parents=True)
            installed.write_bytes(artifact)
        else:
            assert args[1:] == ["--help"]
        return subprocess.CompletedProcess(args, 0, stdout="rustup 1.29.2 (fixture)\n")

    with (
        patch.object(
            bootstrap,
            "runner_paths",
            return_value=tuple(
                Path(hosted[x]) for x in ("HOME", "RUNNER_TEMP", "GITHUB_ENV")
            ),
        ),
        patch.object(bootstrap, "admit_ca", return_value=120),
        patch.object(bootstrap, "SHA256", hashlib.sha256(artifact).hexdigest()),
        patch.object(bootstrap.subprocess, "run", side_effect=stage),
    ):
        bootstrap.bootstrap({**hosted, "RUSTUP_FORCE_ARG0": "old-manager-mode"})
    assert len(calls) == 3  # curl, verified offline stage, help; no SDK/self-update.
    assert "BASH_ENV=" in Path(hosted["GITHUB_ENV"]).read_text()


def test_later_actual_bash_step_clears_dispatch_but_preserves_proxy(tmp_path):
    startup = tmp_path / "startup.sh"
    startup.write_text(bootstrap.startup_script(tmp_path))
    dirty = {
        "RUSTUP_FORCE_ARG0": "cargo",
        "RUSTUP_TOOLCHAIN": "untrusted",
        "RUST_TRACE_DIR": "/untrusted",
        "RUSTC_WRAPPER": "/untrusted",
        "RUSTDOC": "/untrusted",
        "CARGO_BUILD_RUSTC": "/untrusted",
        "CARGO_ENCODED_RUSTFLAGS": "untrusted",
        "CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER": "/untrusted",
        "CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUNNER": "/untrusted",
        "HTTPS_PROXY": "http://proxy.example",
        "CARGO_HTTP_SSL_VERIFY": "false",
    }
    helper_clean = bootstrap.clean_environment(dirty, tmp_path)
    for key in dirty:
        if key not in {"HTTPS_PROXY", "CARGO_HTTP_SSL_VERIFY"}:
            assert key not in helper_clean
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            f"'{sys.executable}' -c 'import os,json; print(json.dumps(dict(os.environ)))'",
        ],
        env={**os.environ, **dirty, "BASH_ENV": str(startup)},
        check=True,
        capture_output=True,
        text=True,
    )
    import json

    clean = json.loads(result.stdout)
    for key in dirty:
        if key not in {"HTTPS_PROXY", "CARGO_HTTP_SSL_VERIFY"}:
            assert key not in clean
    assert clean["HTTPS_PROXY"] == dirty["HTTPS_PROXY"]
    assert clean["CARGO_HTTP_SSL_VERIFY"] == "true"
    assert clean["RUSTUP_HOME"] == str(tmp_path / ".rustup")
    assert clean["CARGO_HOME"] == str(tmp_path / ".cargo")
    assert clean["PATH"].split(":")[0] == str(tmp_path / ".cargo/bin")
    assert "BASH_ENV" not in clean
    nested = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            "export RUSTUP_TOOLCHAIN=intended-by-proxy; /bin/bash -c 'test \"$RUSTUP_TOOLCHAIN\" = intended-by-proxy'",
        ],
        env={**os.environ, "BASH_ENV": str(startup)},
        check=False,
    )
    assert nested.returncode == 0
