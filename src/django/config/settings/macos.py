"""
macOS Configurations

- Uses SQLite for database
- Uses file-based cache
- Configured for direct access without SSL/TLS
- Includes whitenoise for static file serving
"""

# Import the base settings
from .base import *  # noqa

DEBUG = False

# SECRET CONFIGURATION
# ------------------------------------------------------------------------------
# See: https://docs.djangoproject.com/en/dev/ref/settings/#secret-key
# Raises ImproperlyConfigured exception if DJANGO_SECRET_KEY not in os.environ
SECRET_KEY = env("DJANGO_SECRET_KEY")  # noqa

# Use Whitenoise to serve static files
# See: https://whitenoise.readthedocs.io/
WHITENOISE_MIDDLEWARE = [
    "whitenoise.middleware.WhiteNoiseMiddleware",
]
MIDDLEWARE = WHITENOISE_MIDDLEWARE + MIDDLEWARE  # noqa

# SITE CONFIGURATION
# ------------------------------------------------------------------------------
# Hosts/domain names that are valid for this site
# See https://docs.djangoproject.com/en/dev/ref/settings/#allowed-hosts
ALLOWED_HOSTS = env.list(  # noqa
    "DJANGO_ALLOWED_HOSTS",
    default=[
        "localhost",
        "127.0.0.1",
    ],
)
# END SITE CONFIGURATION

INSTALLED_APPS += [  # noqa
    "granian",
]

# Static Assets
# ------------------------
STORAGES["staticfiles"]["BACKEND"] = (  # noqa
    "whitenoise.storage.CompressedManifestStaticFilesStorage"
)

# TEMPLATE CONFIGURATION
# ------------------------------------------------------------------------------
# Make sure APP_DIRS is True to find templates in installed packages
TEMPLATES[0]["APP_DIRS"] = True  # noqa

# DATABASE CONFIGURATION
# ------------------------------------------------------------------------------
# Use SQLite in production with DATABASE_URL
if env("DATABASE_URL", default=None):  # noqa
    DATABASES["default"] = env.db("DATABASE_URL")  # noqa
# Otherwise use the default SQLite configuration from base.py

# CACHING
# ------------------------------------------------------------------------------
# Caching
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": env("DJANGO_CACHE_LOCATION"),  # noqa
        "TIMEOUT": 600,
        "OPTIONS": {"MAX_ENTRIES": 10000},
    }
}
CACHE_MIDDLEWARE_ALIAS = "default"
CACHE_MIDDLEWARE_SECONDS = 600
CACHE_MIDDLEWARE_KEY_PREFIX = "nyxmon"

# Logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "root": {
        "level": "WARNING",
        "handlers": [
            "console",
        ],
    },
    "formatters": {
        "verbose": {
            "format": "%(levelname)s %(asctime)s %(module)s "
            "%(process)d %(thread)d %(message)s"
        },
    },
    "handlers": {
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "loggers": {
        "django.db.backends": {
            "level": "ERROR",
            "handlers": [
                "console",
            ],
            "propagate": False,
        },
        "django.security.DisallowedHost": {
            "level": "ERROR",
            "handlers": [
                "console",
            ],
            "propagate": False,
        },
    },
}

# Custom Admin URL, use {% url 'admin:index' %}
ADMIN_URL = env("DJANGO_ADMIN_URL")  # noqa
