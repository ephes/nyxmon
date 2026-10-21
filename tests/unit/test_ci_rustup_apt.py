"""CA-only signed APT caller guards; no local package/network mutation."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ci_rustup.py"
spec = importlib.util.spec_from_file_location("ci_rustup_apt_fixture", SCRIPT)
assert spec and spec.loader
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
TARGET = "20260816ubuntu0.24.04.1"
BEFORE = {"ca-certificates": ("20240203", "ii"), "keep": ("1.2", "ii")}
AFTER = {**BEFORE, "ca-certificates": (TARGET, "ii")}
SIMULATION = (
    f"Inst ca-certificates [20240203] ({TARGET} Ubuntu:24.04/noble-security [all])\n"
    f"Conf ca-certificates ({TARGET} Ubuntu:24.04/noble-security [all])\n"
)


@pytest.mark.parametrize("release,target", helper.CA_TARGETS.items())
def test_supported_ubuntu_mapping(tmp_path: Path, release: str, target: str) -> None:
    os_release = tmp_path / "os-release"
    os_release.write_text(f'ID=ubuntu\nVERSION_ID="{release}"\n')
    assert helper.ubuntu_ca_target(os_release) == target


@pytest.mark.parametrize(
    "identity", ["ID=debian\nVERSION_ID=12", "ID=ubuntu\nVERSION_ID=26.04", "ID=ubuntu"]
)
def test_unsupported_os_fails(tmp_path: Path, identity: str) -> None:
    os_release = tmp_path / "os-release"
    os_release.write_text(identity)
    with pytest.raises(RuntimeError, match=r"Ubuntu 22\.04 or 24\.04"):
        helper.ubuntu_ca_target(os_release)


@pytest.mark.parametrize(
    "extra",
    ["Remv keep [1.2]", "Inst new (1.0 Ubuntu [all])", "Conf keep (1.3 Ubuntu [all])"],
)
def test_simulation_rejects_every_unrelated_action(extra: str) -> None:
    with pytest.raises(RuntimeError, match="non-allowlisted"):
        helper.validate_ca_transaction(SIMULATION + extra + "\n", TARGET)


def test_simulation_requires_exact_target_and_upgrade_configure_pair() -> None:
    helper.validate_ca_transaction(SIMULATION, TARGET)
    for bad in ["", SIMULATION.replace(TARGET, "old"), SIMULATION.splitlines()[0]]:
        with pytest.raises(RuntimeError):
            helper.validate_ca_transaction(bad, TARGET)


def test_current_package_skips_all_apt_calls() -> None:
    with (
        patch.object(helper, "ubuntu_ca_target", return_value=TARGET),
        patch.object(helper, "package_state", return_value=AFTER),
        patch.object(
            helper.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ) as run,
    ):
        helper.refresh_runner_ca({"APT_CONFIG": "/untrusted"})
    assert run.call_count == 1
    assert run.call_args.args[0][:2] == ["/usr/bin/dpkg", "--compare-versions"]
    assert "APT_CONFIG" not in run.call_args.kwargs["env"]


def test_exact_signed_apt_transaction_and_non_target_parity() -> None:
    calls: list[list[str]] = []

    def cli(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert "APT_CONFIG" not in kwargs["env"]
        assert kwargs["env"]["HTTPS_PROXY"] == "http://trusted-proxy.example"
        if args[0] == "/usr/bin/dpkg":
            return subprocess.CompletedProcess(args, 1)
        assert args[:2] == ["/usr/bin/sudo", "-n"]
        for option in [
            "Acquire::https::Verify-Peer=true",
            "Acquire::https::Verify-Host=true",
            "Acquire::AllowInsecureRepositories=false",
            "APT::Get::AllowUnauthenticated=false",
        ]:
            assert option in args
        return subprocess.CompletedProcess(
            args, 0, stdout=SIMULATION if "--simulate" in args else ""
        )

    with (
        patch.object(helper, "ubuntu_ca_target", return_value=TARGET),
        patch.object(helper, "package_state", side_effect=[BEFORE, AFTER]),
        patch.object(helper.subprocess, "run", side_effect=cli),
    ):
        helper.refresh_runner_ca(
            {"APT_CONFIG": "/untrusted", "HTTPS_PROXY": "http://trusted-proxy.example"}
        )
    assert len(calls) == 4
    assert calls[1][-1] == "update"
    simulation, actual = calls[2:]
    assert "--no-remove" in simulation and "--only-upgrade" in simulation
    assert "--no-install-recommends" in simulation
    assert simulation[-1] == f"ca-certificates={TARGET}"
    assert [arg for arg in simulation if arg != "--simulate"] == [
        arg for arg in actual if arg != "--assume-yes"
    ]


@pytest.mark.parametrize("after", [{**AFTER, "keep": ("1.3", "ii")}, BEFORE])
def test_post_transaction_package_drift_refused(
    after: dict[str, tuple[str, str]],
) -> None:
    responses = [
        subprocess.CompletedProcess([], 1),
        subprocess.CompletedProcess([], 0),
        subprocess.CompletedProcess([], 0, stdout=SIMULATION),
        subprocess.CompletedProcess([], 0),
    ]
    with (
        patch.object(helper, "ubuntu_ca_target", return_value=TARGET),
        patch.object(helper, "package_state", side_effect=[BEFORE, after]),
        patch.object(helper.subprocess, "run", side_effect=responses),
    ):
        with pytest.raises(RuntimeError, match=r"package|transaction"):
            helper.refresh_runner_ca({})


def test_extra_action_never_executes_actual_install() -> None:
    responses = [
        subprocess.CompletedProcess([], 1),
        subprocess.CompletedProcess([], 0),
        subprocess.CompletedProcess([], 0, stdout=SIMULATION + "Remv keep [1.2]\n"),
    ]
    with (
        patch.object(helper, "ubuntu_ca_target", return_value=TARGET),
        patch.object(helper, "package_state", return_value=BEFORE),
        patch.object(helper.subprocess, "run", side_effect=responses) as run,
    ):
        with pytest.raises(RuntimeError, match="non-allowlisted"):
            helper.refresh_runner_ca({})
        assert run.call_count == 3


def test_package_failure_prevents_fingerprint_admission_and_download(
    tmp_path: Path,
) -> None:
    with (
        patch.object(
            helper, "runner_paths", return_value=(tmp_path, tmp_path, tmp_path / "env")
        ),
        patch.object(
            helper,
            "refresh_runner_ca",
            side_effect=subprocess.CalledProcessError(100, ["apt-get"]),
        ),
        patch.object(helper, "admit_ca") as admission,
        patch.object(helper.subprocess, "run") as run,
    ):
        with pytest.raises(subprocess.CalledProcessError):
            helper.bootstrap({})
        admission.assert_not_called()
        run.assert_not_called()


def test_unchanged_strict_store_gate_runs_after_package_refresh(tmp_path: Path) -> None:
    events: list[str] = []

    def fail_admission(path: Path) -> int:
        events.append("fingerprints")
        raise RuntimeError("retains incident roots")

    with (
        patch.object(
            helper, "runner_paths", return_value=(tmp_path, tmp_path, tmp_path / "env")
        ),
        patch.object(
            helper,
            "refresh_runner_ca",
            side_effect=lambda env: events.append("package"),
        ),
        patch.object(helper, "admit_ca", side_effect=fail_admission),
        patch.object(helper.subprocess, "run") as run,
    ):
        with pytest.raises(RuntimeError, match="incident roots"):
            helper.bootstrap({})
        run.assert_not_called()
    assert events == ["package", "fingerprints"]
