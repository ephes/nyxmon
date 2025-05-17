# ruff: noqa: F405
from .base import *  # noqa F403

DEBUG = env.bool("DJANGO_DEBUG", default=True)
if len(TEMPLATES) > 0:
    TEMPLATES[0]["OPTIONS"]["debug"] = DEBUG

SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    default="4778ec85e3aab5b1276a053fc98ee43e61b8b19ea57a59ffae026b48e95c7be5",
)

EMAIL_PORT = 1025
EMAIL_HOST = "localhost"
EMAIL_BACKEND = env(
    "DJANGO_EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend"
)

MIDDLEWARE += [
    "debug_toolbar.middleware.DebugToolbarMiddleware",
]
INSTALLED_APPS += [
    "debug_toolbar",
]
INTERNAL_IPS = [
    "127.0.0.1",
]

ALLOWED_HOSTS = env.list(
    "DJANGO_ALLOWED_HOSTS",
    default=[
        "*",
    ],
)
