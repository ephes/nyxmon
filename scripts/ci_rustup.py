"""Pinned manager bootstrap for disposable GitHub-hosted Linux x86_64 jobs only."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import ssl
import subprocess
import tempfile
from pathlib import Path

VERSION = "1.29.2"
SHA256 = "5be191e8c70a5007074254cb74d78dbfdd25cd5a61c27de7b8e5410122ec24f4"
URL = f"https://static.rust-lang.org/rustup/archive/{VERSION}/x86_64-unknown-linux-gnu/rustup-init"
CA_FILE = Path("/etc/ssl/certs/ca-certificates.crt")
BLOCKED = {
    "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4",
    "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b",
}
DISPATCH = {"RUSTC", "RUSTDOC", "RUSTFLAGS", "RUSTDOCFLAGS", "CARGO_HOME"}


def dispatch_variable(name: str) -> bool:
    return (
        name.startswith(("RUSTUP_", "RUST_", "RUSTC_", "RUSTDOC_"))
        or name in DISPATCH
        or (
            name.startswith("CARGO_")
            and (
                any(part in name for part in ("RUSTC", "RUSTDOC", "RUSTFLAGS"))
                or (
                    name.startswith("CARGO_TARGET_")
                    and name.endswith(("_LINKER", "_RUNNER"))
                )
            )
        )
    )


def runner_paths(env: dict[str, str]) -> tuple[Path, Path, Path]:
    if (
        env.get("GITHUB_ACTIONS") != "true"
        or env.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or env.get("RUNNER_OS") != "Linux"
        or platform.system() != "Linux"
        or platform.machine() != "x86_64"
    ):
        raise RuntimeError("requires a disposable GitHub-hosted Linux x86_64 runner")
    if env.get("BASH_ENV"):
        raise RuntimeError("refusing to replace an existing BASH_ENV")
    paths = tuple(Path(env[name]) for name in ("HOME", "RUNNER_TEMP", "GITHUB_ENV"))
    if any(not p.is_absolute() or any(c in str(p) for c in "\r\n") for p in paths):
        raise RuntimeError("runner paths must be absolute single-line paths")
    return paths[0], paths[1], paths[2]


def admit_ca(path: Path) -> int:
    text = path.read_text(encoding="ascii")
    certificates = re.findall(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", text, re.S
    )
    if not certificates:
        raise RuntimeError("runner CA bundle contains no certificates")
    digests = {
        hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
        for pem in certificates
    }
    if digests & BLOCKED:
        raise RuntimeError(
            "runner OS CA bundle retains incident roots; update the runner image separately"
        )
    return len(certificates)


def clean_environment(env: dict[str, str], home: Path) -> dict[str, str]:
    clean = {key: value for key, value in env.items() if not dispatch_variable(key)}
    clean.update(
        CARGO_HOME=str(home / ".cargo"),
        RUSTUP_HOME=str(home / ".rustup"),
        CARGO_HTTP_CAINFO=str(CA_FILE),
        CARGO_HTTP_SSL_VERIFY="true",
        SSL_CERT_FILE=str(CA_FILE),
    )
    return clean


def startup_script(home: Path) -> str:
    # Bash starts every subsequent workflow run step with BASH_ENV. Unset even
    # previously unknown dispatch variables rather than exporting empty values.
    import shlex

    return (
        """while IFS= read -r name; do
  case "$name" in
    RUSTUP_*|RUST_*|RUSTC_*|RUSTDOC_*|RUSTC|RUSTDOC|RUSTFLAGS|RUSTDOCFLAGS|CARGO_HOME|CARGO_*RUSTC*|CARGO_*RUSTDOC*|CARGO_*RUSTFLAGS*|CARGO_TARGET_*_LINKER|CARGO_TARGET_*_RUNNER) unset "$name" ;;
  esac
done < <(compgen -e)
unset name BASH_ENV
"""
        + "\n".join(
            f"export {name}={shlex.quote(value)}"
            for name, value in clean_environment({}, home).items()
        )
        + f'\nexport PATH={shlex.quote(str(home / ".cargo/bin"))}:"$PATH"\n'
    )


def bootstrap(env: dict[str, str]) -> None:
    home, temporary, github_env = runner_paths(env)
    count = admit_ca(CA_FILE)  # Admission precedes curl and any manager execution.
    clean = clean_environment(env, home)
    with tempfile.TemporaryDirectory(
        prefix="verified-rustup-", dir=temporary
    ) as scratch:
        candidate = Path(scratch) / "rustup-init"
        subprocess.run(
            [
                "/usr/bin/curl",
                "-q",
                "--fail",
                "--location",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--tlsv1.2",
                "--max-time",
                "90",
                "--cacert",
                str(CA_FILE),
                "--output",
                str(candidate),
                URL,
            ],
            env=clean,
            check=True,
            timeout=100,
        )
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != SHA256:
            raise RuntimeError("official Rustup artifact checksum mismatch")
        candidate.chmod(0o700)
        # This upstream stage copies the verified manager/proxies, without
        # invoking the old manager or downloading a toolchain.
        subprocess.run(
            [str(candidate), "--self-replace"], env=clean, check=True, timeout=60
        )
    installed = home / ".cargo/bin/rustup"
    if hashlib.sha256(installed.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError("installed Rustup checksum mismatch")
    header = subprocess.run(
        [str(installed), "--help"],
        env=clean,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.splitlines()[0]
    if not header.startswith(f"rustup {VERSION} "):
        raise RuntimeError("installed Rustup help version mismatch")
    # Publish only after manager and bundle verification; no persistent user config.
    descriptor, filename = tempfile.mkstemp(
        prefix="verified-rust-env-", suffix=".sh", dir=temporary
    )
    with os.fdopen(descriptor, "w") as stream:
        stream.write(startup_script(home))
    with github_env.open("a") as stream:
        stream.write(f"BASH_ENV={filename}\n")
    print(
        f"Verified Rustup {VERSION}, SHA256 {SHA256}; admitted {count} OS CA certificates"
    )


if __name__ == "__main__":
    bootstrap(dict(os.environ))
