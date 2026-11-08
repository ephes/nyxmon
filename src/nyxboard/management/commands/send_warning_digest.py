"""Send all open warnings as one silent Telegram message.

Meant for a timer together with ``NYXMON_NOTIFY_WARNING_DELIVERY=digest``. Sends
nothing when no warning is open.

Usage:
    python manage.py send_warning_digest
    python manage.py send_warning_digest --dry-run
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from django.core.management.base import BaseCommand, CommandError
from django.db.models import OuterRef, Subquery

from nyxboard.models import HealthCheck, Result
from nyxboard.timestamps import usable_epoch
from nyxmon.adapters.notification import AsyncTelegramNotifier
from nyxmon.domain import ResultStatus

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 3800
MAX_ERROR_CHARS = 200

# Result data that means "not an open warning" (agent decision branches).
INERT_RESULT_KEYS = ("collector_internal", "notification_suppressed")


@dataclass(frozen=True, slots=True)
class OpenWarning:
    name: str
    since: str
    error: str


def open_warnings() -> list[OpenWarning]:
    """Return enabled checks whose newest result is an active warning."""
    latest = Result.objects.filter(health_check_id=OuterRef("pk")).order_by(
        "-created_at", "-id"
    )
    # One statement binds each result to its incident state in the same snapshot.
    # Project only warning payloads; do not hydrate checks or historical results.
    rows = (
        HealthCheck.objects.filter(disabled=False)
        .annotate(
            latest_status=Subquery(latest.values("status")[:1]),
            latest_data=Subquery(latest.values("data")[:1]),
        )
        .filter(latest_status=ResultStatus.WARNING)
        .order_by("name", "pk")
        .values_list(
            "pk",
            "name",
            "latest_data",
            "notification_state__first_failure_at",
        )
    )
    warnings = []
    for check_id, name, payload, first_failure_at in rows:
        data = payload if isinstance(payload, dict) else {}
        if any(data.get(key) for key in INERT_RESULT_KEYS):
            continue
        epoch = usable_epoch(first_failure_at)
        if epoch is None and first_failure_at not in (None, 0):
            logger.warning("Invalid first_failure_at for check %s", check_id)
        since = (
            datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d")
            if epoch is not None
            else "unknown"
        )
        warnings.append(
            OpenWarning(name, since, str(data.get("error_msg", ""))[:MAX_ERROR_CHARS])
        )
    return warnings


def render(warnings: list[OpenWarning], escape=lambda text: text) -> str:
    """Render the digest, leaving out what Telegram's size limit cannot carry."""
    count = len(warnings)
    noun = "warning" if count == 1 else "warnings"
    text = escape(f"⚠️ Nyxmon digest: {count} open {noun}")
    for position, warning in enumerate(warnings):
        line = f"\n\n{escape(f'{warning.name} (since {warning.since})')}"
        if warning.error:
            line += f"\n{escape(warning.error)}"
        if len(text) + len(line) > MAX_MESSAGE_CHARS:
            return (
                text + "\n\n" + escape(f"... and {count - position} more in NyxBoard")
            )
        text += line
    return text


class Command(BaseCommand):
    help = "Send all open warnings as one silent Telegram message."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the digest instead of sending it",
        )

    def handle(self, *args, **options):
        warnings = open_warnings()
        if not warnings:
            self.stdout.write("No open warnings; nothing to send.")
            return
        if options["dry_run"]:
            self.stdout.write(render(warnings))
            return
        notifier = AsyncTelegramNotifier()
        text = render(warnings, notifier.escape_markdown_v2)
        delivered = asyncio.run(notifier.async_send(text, high_priority=False))
        if delivered is None:
            raise CommandError("Telegram credentials are missing; digest not sent")
        if delivered is False:
            raise CommandError("Telegram did not accept the digest")
        self.stdout.write(f"Sent a digest of {len(warnings)} open warnings.")
