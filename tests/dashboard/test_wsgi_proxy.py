"""Pin the production entrypoint's client-address contract for login throttling."""

import runpy

import pytest
from django.core.handlers.wsgi import WSGIRequest
from django.test import RequestFactory

from nyxboard.login_throttle import client_address


@pytest.mark.parametrize(
    "peer, expected",
    [
        ("127.0.0.1", "198.51.100.7"),
        ("::1", "198.51.100.7"),
        ("192.0.2.10", "192.0.2.10"),
    ],
)
def test_wrapper_entrypoint_sets_throttle_address(
    monkeypatch, settings, peer, expected
):
    settings.NYXBOARD_LOGIN_PROXY_HOPS = 0
    seen = []

    def downstream(environ, start_response):
        seen.append((environ["REMOTE_ADDR"], client_address(WSGIRequest(environ))))
        start_response("200 OK", [])
        return [b"ok"]

    monkeypatch.setattr("django.core.wsgi.get_wsgi_application", lambda: downstream)
    # Load the actual entrypoint with a recording downstream app, retaining the
    # real Granian wrapper and the entrypoint's configured trusted hosts.
    application = runpy.run_module("config.wsgi_proxy")["application"]
    environ = (
        RequestFactory()
        .get(
            "/accounts/login/",
            REMOTE_ADDR=peer,
            HTTP_X_FORWARDED_FOR="203.0.113.9, 198.51.100.7",
        )
        .environ
    )
    statuses = []

    assert list(
        application(environ, lambda status, headers: statuses.append(status))
    ) == [b"ok"]
    assert statuses == ["200 OK"]
    assert seen == [(expected, expected)]
