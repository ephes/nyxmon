"""Pin the security contract of config.settings.production.

The module is loaded in a fresh process with the environment the ops-library
``nyxmon_deploy`` role renders, then exercised through the real
``config.wsgi_proxy`` entrypoint the way the reverse proxy and loopback health
probes reach it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.urls import reverse

DJANGO_DIR = Path(__file__).resolve().parents[2] / "src" / "django"
HOST = "nyxmon.example.test"

# Runs in the child process: build one WSGI request per case and report the
# status, Location and Set-Cookie headers the entrypoint answers with.
PROBE = """
import io, json, sys
from django.conf import settings
from django.core.management import call_command
from config.wsgi_proxy import application

# The checkout has no collected static files; the manifest storage would
# refuse to render the login pages without them.
settings.STORAGES["staticfiles"]["BACKEND"] = (
    "django.contrib.staticfiles.storage.StaticFilesStorage"
)
call_command("migrate", verbosity=0)
out = {"settings": {n: getattr(settings, n) for n in json.loads(sys.argv[1])}}
for name, case in json.loads(sys.argv[2]).items():
    environ = {
        "REQUEST_METHOD": "GET", "PATH_INFO": case["path"], "QUERY_STRING": "",
        "SERVER_NAME": "127.0.0.1", "SERVER_PORT": "10017",
        "SERVER_PROTOCOL": "HTTP/1.1", "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(), "wsgi.errors": sys.stderr,
        "REMOTE_ADDR": case.get("peer", "127.0.0.1"),
        "HTTP_HOST": case.get("host", "127.0.0.1:10017"),
    }
    environ.update(case.get("headers", {}))
    seen = {}
    def start_response(status, headers, exc_info=None):
        seen["status"] = int(status.split()[0])
        seen["location"] = dict(headers).get("Location")
        seen["hsts"] = dict(headers).get("Strict-Transport-Security")
        seen["cookies"] = [v for k, v in headers if k == "Set-Cookie"]
    b"".join(application(environ, start_response))
    out[name] = seen
print(json.dumps(out))
"""

PINNED = [
    "DEBUG",
    "ALLOWED_HOSTS",
    "SECURE_SSL_REDIRECT",
    "SECURE_HSTS_SECONDS",
    "SECURE_HSTS_INCLUDE_SUBDOMAINS",
    "SECURE_CONTENT_TYPE_NOSNIFF",
    "SESSION_COOKIE_SECURE",
    "SESSION_COOKIE_HTTPONLY",
    "CSRF_COOKIE_SECURE",
    "CSRF_COOKIE_HTTPONLY",
    "X_FRAME_OPTIONS",
    "SECURE_PROXY_SSL_HEADER",
    "USE_X_FORWARDED_HOST",
    "NYXBOARD_REQUIRE_LOGIN",
    "NYXBOARD_LOGIN_PROXY_HOPS",
]

HTTPS = {"HTTP_X_FORWARDED_PROTO": "https"}
CASES = {
    "plain_probe": {"path": "/admin/login/"},
    "https_probe": {"path": "/admin/login/", "headers": HTTPS},
    "proxied_dashboard": {"path": "/", "host": HOST, "headers": HTTPS},
    "proxied_login": {"path": "/accounts/login/", "host": HOST, "headers": HTTPS},
    "bogus_host": {
        "path": "/accounts/login/",
        "host": "evil.example",
        "headers": HTTPS,
    },
    "bogus_host_with_forwarded_host": {
        "path": "/accounts/login/",
        "host": "evil.example",
        "headers": {**HTTPS, "HTTP_X_FORWARDED_HOST": HOST},
    },
    "untrusted_peer_claims_https": {
        "path": "/accounts/login/",
        "host": HOST,
        "peer": "192.0.2.10",
        "headers": HTTPS,
    },
}


@pytest.fixture(scope="module")
def production(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("production-settings")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("DJANGO_", "NYXBOARD_"))
    }
    env["PYTHONPATH"] = os.pathsep.join(
        [str(DJANGO_DIR), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    # The variables nyxmon.env.j2 renders for the web service, plus a
    # throwaway database so the checkout stays clean.
    env.update(
        DJANGO_SETTINGS_MODULE="config.settings.production",
        DJANGO_SECRET_KEY="test-only-production-settings",
        DJANGO_ALLOWED_HOSTS=f"127.0.0.1,{HOST}",
        DJANGO_CACHE_LOCATION=str(tmp_path / "cache"),
        DJANGO_ADMIN_URL="admin/",
        DJANGO_DEBUG="false",
        NYXBOARD_REQUIRE_LOGIN="true",
        DATABASE_URL=f"sqlite:///{tmp_path / 'db.sqlite3'}",
    )
    result = subprocess.run(
        [sys.executable, "-c", PROBE, json.dumps(PINNED), json.dumps(CASES)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_production_settings_pin_security_values(production):
    assert production["settings"] == {
        "DEBUG": False,
        "ALLOWED_HOSTS": ["127.0.0.1", HOST],
        "SECURE_SSL_REDIRECT": True,
        "SECURE_HSTS_SECONDS": 15552000,
        "SECURE_HSTS_INCLUDE_SUBDOMAINS": True,
        "SECURE_CONTENT_TYPE_NOSNIFF": True,
        "SESSION_COOKIE_SECURE": True,
        "SESSION_COOKIE_HTTPONLY": True,
        "CSRF_COOKIE_SECURE": True,
        "CSRF_COOKIE_HTTPONLY": True,
        "X_FRAME_OPTIONS": "DENY",
        # Forwarded headers are trusted in config.wsgi_proxy only.
        "SECURE_PROXY_SSL_HEADER": None,
        "USE_X_FORWARDED_HOST": False,
        "NYXBOARD_REQUIRE_LOGIN": True,
        "NYXBOARD_LOGIN_PROXY_HOPS": 0,
    }


def test_plain_http_is_redirected_to_https(production):
    assert production["plain_probe"]["status"] == 301
    assert production["plain_probe"]["location"] == (
        "https://127.0.0.1:10017/admin/login/"
    )


def test_loopback_probe_with_forwarded_proto_answers(production):
    assert production["https_probe"]["status"] == 200
    assert production["https_probe"]["hsts"] == (
        "max-age=15552000; includeSubDomains; preload"
    )


def test_proxied_requests_reach_the_login(production):
    assert production["proxied_dashboard"]["status"] == 302
    assert production["proxied_dashboard"]["location"] == "/accounts/login/?next=/"
    login = production["proxied_login"]
    assert login["status"] == 200
    (csrf_cookie,) = login["cookies"]
    assert csrf_cookie.strip().startswith("csrftoken=")
    assert "; Secure" in csrf_cookie


@pytest.mark.parametrize("case", ["bogus_host", "bogus_host_with_forwarded_host"])
def test_unknown_host_is_rejected(production, case):
    assert production[case]["status"] == 400


def test_forwarded_proto_from_untrusted_peer_is_ignored(production):
    response = production["untrusted_peer_claims_https"]
    assert response["status"] == 301
    assert response["location"] == f"https://{HOST}/accounts/login/"


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
