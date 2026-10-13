"""Load production settings in a fresh process to avoid mutating test settings."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("value, expected", [(None, 15552000), ("600", 600), ("0", 0)])
def test_production_hsts_max_age(value, expected, tmp_path):
    env = os.environ.copy()
    django_dir = Path(__file__).resolve().parents[2] / "src" / "django"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(django_dir), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    env.update(
        DJANGO_SECRET_KEY="test-only-production-settings",
        DJANGO_CACHE_LOCATION=str(tmp_path),
        DJANGO_ADMIN_URL="admin/",
    )
    env.pop("DJANGO_SECURE_HSTS_SECONDS", None)
    if value is not None:
        env["DJANGO_SECURE_HSTS_SECONDS"] = value
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from config.settings.production import SECURE_HSTS_SECONDS; print(SECURE_HSTS_SECONDS)",
        ],
        env=env,
        cwd=django_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) == expected
