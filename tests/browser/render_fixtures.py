# ruff: noqa: E402
"""Render production pages against an isolated synthetic in-memory database."""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/django"))
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test"

import django
from django.conf import settings

settings.DATABASES["default"]["NAME"] = ":memory:"
settings.ALLOWED_HOSTS = ["testserver"]
django.setup()

from django.contrib.staticfiles import finders
from django.core.management import call_command
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse
from nyxboard.models import HealthCheck, Service

call_command("migrate", verbosity=0)
service = Service.objects.create(name="Synthetic service")
checks = [
    HealthCheck.objects.create(
        service=service,
        name=name,
        check_type="http",
        url="https://example.test",
        check_interval=300,
        next_check_time=4102444800,
    )
    for name in ["Alpha", "Beta"]
]
client = Client()
html = client.get(reverse("nyxboard:dashboard")).content.decode()
fragments = {}
for check in checks:
    for disabled in [False, True]:
        check.disabled = disabled
        fragments[f"{check.id}-{disabled}"] = render_to_string(
            "nyxboard/partials/healthcheck-card.html",
            {"check": check, "check_mode": "normal"},
        )
checks[0].disabled = False
checks[0].status = "processing"
fragments["1-processing"] = render_to_string(
    "nyxboard/partials/healthcheck-card.html", {"check": checks[0], "check_mode": "due"}
)
# Serve exactly the shipped HTMX, not a test double or downloaded replacement.
import re

htmx_url = re.search(r'<script src="([^"]*htmx[^"]*)"', html).group(1)
static_path = htmx_url.removeprefix(settings.STATIC_URL)
print(
    json.dumps(
        {
            "html": html,
            "fragments": fragments,
            "htmxUrl": htmx_url,
            "htmx": Path(finders.find(static_path)).read_text(),
        }
    )
)
