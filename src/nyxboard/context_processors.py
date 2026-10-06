"""Template context processors for NyxBoard."""

from django.http import HttpRequest

from .auth import login_required_enabled


def nyxboard_auth(request: HttpRequest) -> dict[str, bool]:
    """Expose whether ``NYXBOARD_REQUIRE_LOGIN`` is enabled to templates."""
    return {"nyxboard_require_login": login_required_enabled()}
