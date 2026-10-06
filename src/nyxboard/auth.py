"""Optional login requirement for the NyxBoard views.

NyxBoard has historically relied entirely on the reverse proxy for access
control: the ops-library Traefik configuration routes LAN/Tailscale clients
straight through and puts HTTP basic auth in front of public clients. The
``NYXBOARD_REQUIRE_LOGIN`` setting adds an application-level layer on top of
that. It defaults to ``False`` so existing deployments keep their behaviour
until the owner turns it on.

When enabled, every view decorated with :func:`nyxboard_login_required`
requires an authenticated Django user:

* Ordinary page requests are redirected to ``settings.LOGIN_URL`` with a
  ``next`` parameter, the same as Django's ``login_required``.
* HTMX requests (``HX-Request: true``) get ``403`` with an ``HX-Redirect``
  header pointing at the login page, so htmx navigates the whole window there
  instead of swapping a login form into a card.
* JSON endpoints get ``403`` with a JSON error body.

``403`` rather than ``401`` is deliberate: a ``401`` must carry a
``WWW-Authenticate`` challenge, and on the public Traefik router a challenge
from the backend could be confused with the proxy's own basic-auth challenge.
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import resolve_url
from django.utils.http import url_has_allowed_host_and_scheme

ViewFunc = Callable[..., HttpResponse]

#: Attribute set on every wrapped view so tests can prove that each NyxBoard
#: URL pattern is covered by the login requirement.
LOGIN_GUARD_ATTR = "nyxboard_login_guarded"


def login_required_enabled() -> bool:
    """Return whether ``NYXBOARD_REQUIRE_LOGIN`` is switched on.

    Read at request time so ``override_settings`` and runtime configuration
    changes take effect without re-importing the views.
    """
    return bool(getattr(settings, "NYXBOARD_REQUIRE_LOGIN", False))


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _htmx_next_path(request: HttpRequest) -> str:
    """Return the page the user should land on after logging in.

    htmx reports the browser's current page in ``HX-Current-URL``. Only a
    same-host URL is honoured; anything else falls back to the dashboard.
    """
    current = request.headers.get("HX-Current-URL", "")
    if current and url_has_allowed_host_and_scheme(
        current,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        parts = urlsplit(current)
        path = parts.path or "/"
        return f"{path}?{parts.query}" if parts.query else path
    return resolve_url("nyxboard:dashboard")


def _htmx_login_redirect(request: HttpRequest) -> HttpResponse:
    login_redirect = redirect_to_login(
        _htmx_next_path(request), resolve_url(settings.LOGIN_URL)
    )
    response = HttpResponse("Authentication required.", status=403)
    response["HX-Redirect"] = login_redirect["Location"]
    return response


def nyxboard_login_required(view: ViewFunc | None = None, *, json: bool = False) -> Any:
    """Require an authenticated user when ``NYXBOARD_REQUIRE_LOGIN`` is on.

    Args:
        view: The view function to wrap. Allows bare ``@nyxboard_login_required``.
        json: The view is a JSON endpoint; unauthenticated callers get a JSON
            ``403`` instead of a redirect.

    Returns:
        The wrapped view, or a decorator when called with keyword arguments only.
    """

    def decorator(func: ViewFunc) -> ViewFunc:
        @wraps(func)
        def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            if not login_required_enabled() or request.user.is_authenticated:
                return func(request, *args, **kwargs)
            if json:
                return JsonResponse(
                    {"status": "error", "message": "Authentication required"},
                    status=403,
                )
            if _is_htmx(request):
                return _htmx_login_redirect(request)
            return redirect_to_login(
                request.get_full_path(), resolve_url(settings.LOGIN_URL)
            )

        setattr(wrapped, LOGIN_GUARD_ATTR, True)
        return wrapped

    if view is not None:
        return decorator(view)
    return decorator
