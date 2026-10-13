"""Load the deployed settings module in a fresh process and pin its cookie contract."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.urls import reverse


def test_deployed_settings_mark_cookies_secure_without_https_enforcement(tmp_path):
    env = os.environ.copy()
    django_dir = Path(__file__).resolve().parents[2] / "src" / "django"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(django_dir), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    env.update(
        DJANGO_SECRET_KEY="test-only-macos-settings",
        DJANGO_CACHE_LOCATION=str(tmp_path),
        DJANGO_ADMIN_URL="admin/",
    )
    script = (
        "import json; from django.conf import global_settings as g; "
        "import config.settings.macos as s; "
        "print(json.dumps({n: getattr(s, n, getattr(g, n)) for n in ("
        "'SESSION_COOKIE_SECURE', 'CSRF_COOKIE_SECURE', 'SECURE_SSL_REDIRECT', "
        "'SECURE_HSTS_SECONDS', 'ALLOWED_HOSTS')}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        cwd=django_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "SESSION_COOKIE_SECURE": True,
        "CSRF_COOKIE_SECURE": True,
        "SECURE_SSL_REDIRECT": False,
        "SECURE_HSTS_SECONDS": 0,
        "ALLOWED_HOSTS": ["*"],
    }


@pytest.mark.django_db
@pytest.mark.parametrize("url_name", ["admin:login", "nyxboard:login"])
def test_plain_http_login_page_probe_still_answers(client, settings, url_name):
    """Loopback health probes GET a login page over HTTP and send no cookie."""
    settings.SESSION_COOKIE_SECURE = True
    settings.CSRF_COOKIE_SECURE = True
    settings.NYXBOARD_REQUIRE_LOGIN = True

    response = client.get(reverse(url_name))

    assert response.status_code == 200
    assert response.cookies["csrftoken"]["secure"] is True


@pytest.mark.django_db
def test_login_session_cookie_is_secure(client, settings, django_user_model):
    settings.SESSION_COOKIE_SECURE = True
    settings.CSRF_COOKIE_SECURE = True
    settings.NYXBOARD_REQUIRE_LOGIN = True
    django_user_model.objects.create_user("operator", password="pw-for-tests")

    response = client.post(
        reverse("nyxboard:login"),
        {"username": "operator", "password": "pw-for-tests"},
        secure=True,
    )

    assert response.status_code == 302
    assert response.cookies["sessionid"]["secure"] is True
