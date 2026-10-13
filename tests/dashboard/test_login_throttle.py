import pytest
from django.contrib.auth import authenticate
from django.core.cache import cache
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def throttle_settings(settings):
    settings.NYXBOARD_LOGIN_FAILURE_LIMIT = 2
    settings.NYXBOARD_LOGIN_ADDRESS_FAILURE_LIMIT = 5
    settings.NYXBOARD_LOGIN_FAILURE_WINDOW_SECONDS = 900
    settings.NYXBOARD_LOGIN_PROXY_HOPS = 0
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def operator(django_user_model):
    return django_user_model.objects.create_superuser(
        "operator", password="test-password"
    )


def attempt(client, url, password="wrong", username="operator", **extra):
    return client.post(url, {"username": username, "password": password}, **extra)


@pytest.mark.parametrize("route", ["nyxboard:login", "admin:login"])
def test_both_logins_stop_before_password_check(client, operator, monkeypatch, route):
    url = reverse(route)
    for _ in range(2):
        assert attempt(client, url).status_code == 200
    checked = []
    monkeypatch.setattr(
        type(operator), "check_password", lambda self, raw: checked.append(raw) or True
    )
    assert attempt(client, url, "test-password").status_code == 200
    assert checked == []
    assert "_auth_user_id" not in client.session


def test_success_resets_pair_but_preserves_address_budget(client, operator):
    url = reverse("nyxboard:login")
    attempt(client, url)
    assert attempt(client, url, "test-password").status_code == 302
    client.logout()
    attempt(client, url)
    assert attempt(client, url, "test-password").status_code == 302
    client.logout()
    for i in range(3):
        attempt(client, url, username=f"missing{i}")
    assert attempt(client, url, "test-password").status_code == 200


def test_trusted_proxy_keys_and_spoofed_left_entry(client, operator, settings):
    settings.NYXBOARD_LOGIN_PROXY_HOPS = 1
    url = reverse("nyxboard:login")
    for i in range(2):
        attempt(client, url, HTTP_X_FORWARDED_FOR=f"203.0.113.{i}, 198.51.100.1")
    assert (
        attempt(
            client,
            url,
            "test-password",
            HTTP_X_FORWARDED_FOR="203.0.113.99, 198.51.100.1",
        ).status_code
        == 200
    )
    assert (
        attempt(
            client, url, "test-password", HTTP_X_FORWARDED_FOR="198.51.100.2"
        ).status_code
        == 302
    )


def test_untrusted_forwarded_header_is_ignored(client, operator):
    url = reverse("nyxboard:login")
    for i in range(2):
        attempt(client, url, HTTP_X_FORWARDED_FOR=f"198.51.100.{i}")
    assert (
        attempt(
            client, url, "test-password", HTTP_X_FORWARDED_FOR="198.51.100.99"
        ).status_code
        == 200
    )
    assert (
        attempt(client, url, "test-password", REMOTE_ADDR="198.51.100.3").status_code
        == 302
    )


def test_expired_window_unlocks(client, operator, monkeypatch):
    import time

    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    url = reverse("nyxboard:login")
    for _ in range(2):
        attempt(client, url)
    assert attempt(client, url, "test-password").status_code == 200
    now += 901
    assert attempt(client, url, "test-password").status_code == 302


def test_authentication_without_request_is_unaffected(operator):
    for _ in range(3):
        assert authenticate(username="operator", password="wrong") is None
    assert authenticate(username="operator", password="test-password") == operator


@pytest.mark.parametrize("backend", ["locmem.LocMemCache", "filebased.FileBasedCache"])
def test_repeated_failure_does_not_extend_window(
    client, operator, monkeypatch, settings, tmp_path, backend
):
    import time

    settings.CACHES = {
        "default": {
            "BACKEND": f"django.core.cache.backends.{backend}",
            "LOCATION": str(tmp_path),
        }
    }
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    url = reverse("nyxboard:login")
    for _ in range(2):
        attempt(client, url)
    now += 899
    assert attempt(client, url, "test-password").status_code == 200
    now += 2
    assert attempt(client, url, "test-password").status_code == 302


def test_address_lockout_does_not_create_new_cache_entries(
    client, operator, monkeypatch, settings
):
    settings.NYXBOARD_LOGIN_ADDRESS_FAILURE_LIMIT = 2
    url = reverse("nyxboard:login")
    for i in range(2):
        attempt(client, url, username=f"missing{i}")
    writes = []
    monkeypatch.setattr(cache, "set", lambda *args, **kwargs: writes.append(args))
    for i in range(10):
        attempt(client, url, username=f"refused{i}")
    assert attempt(client, url, "test-password").status_code == 200
    assert writes == []


def test_zero_pair_limit_disables_throttling(client, operator, settings):
    settings.NYXBOARD_LOGIN_FAILURE_LIMIT = 0
    url = reverse("nyxboard:login")
    for _ in range(6):
        attempt(client, url)
    assert attempt(client, url, "test-password").status_code == 302


@pytest.mark.parametrize(
    "name, value",
    [
        ("NYXBOARD_LOGIN_FAILURE_LIMIT", "-1"),
        ("NYXBOARD_LOGIN_ADDRESS_FAILURE_LIMIT", "0"),
        ("NYXBOARD_LOGIN_ADDRESS_FAILURE_LIMIT", "-1"),
        ("NYXBOARD_LOGIN_FAILURE_WINDOW_SECONDS", "0"),
        ("NYXBOARD_LOGIN_PROXY_HOPS", "-1"),
    ],
)
def test_invalid_throttle_settings_fail_at_load(name, value):
    import os
    import subprocess
    import sys

    from pathlib import Path

    env = os.environ.copy()
    django_dir = Path(__file__).resolve().parents[2] / "src" / "django"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(django_dir), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    for key in list(env):
        if key.startswith("NYXBOARD_LOGIN_"):
            del env[key]
    env[name] = value
    result = subprocess.run(
        [sys.executable, "-c", "import config.settings.base"],
        env=env,
        cwd=django_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert f"{name} must be at least" in result.stderr


@pytest.mark.parametrize(
    "hops, forwarded, expected",
    [
        (1, "invalid", "127.0.0.1"),
        (2, "198.51.100.1", "127.0.0.1"),
        (2, "203.0.113.7, 198.51.100.1, 10.0.0.1", "198.51.100.1"),
    ],
)
def test_proxy_fallback_and_multiple_hops(settings, hops, forwarded, expected):
    from django.test import RequestFactory
    from nyxboard.login_throttle import client_address

    settings.NYXBOARD_LOGIN_PROXY_HOPS = hops
    request = RequestFactory().post("/", HTTP_X_FORWARDED_FOR=forwarded)
    assert client_address(request) == expected
