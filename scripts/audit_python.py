"""Audit the frozen runtime and development graph without installing it."""

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def main() -> None:
    with TemporaryDirectory(prefix="nyxmon-audit-") as directory:
        requirements = Path(directory) / "requirements.txt"
        with requirements.open("w") as output:
            subprocess.run(
                [
                    "uv",
                    "export",
                    "--frozen",
                    "--all-extras",
                    "--all-groups",
                    "--no-emit-project",
                    "--no-hashes",
                ],
                stdout=output,
                check=True,
            )
        subprocess.run(
            [
                "uvx",
                "pip-audit==2.10.1",
                "--disable-pip",
                "--no-deps",
                "--cache-dir",
                str(Path(directory) / "cache"),
                "-r",
                str(requirements),
            ],
            check=True,
        )


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
