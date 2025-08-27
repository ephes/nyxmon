"""
WSGI application wrapper for Granian with proxy headers support.

This wrapper ensures that Granian properly handles X-Forwarded-Proto
and other proxy headers when running behind Traefik reverse proxy.
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

from django.core.wsgi import get_wsgi_application
from granian.utils.proxies import wrap_wsgi_with_proxy_headers

# Create the Django WSGI application
_app = get_wsgi_application()

# Trust proxy connections from loopback addresses
# Traefik connects to Granian via 127.0.0.1 (loopback)
TRUSTED = ["127.0.0.1", "::1"]

# Wrap the Django app with Granian's proxy headers handler
# This will properly set request.scheme based on X-Forwarded-Proto
application = wrap_wsgi_with_proxy_headers(_app, trusted_hosts=TRUSTED)
