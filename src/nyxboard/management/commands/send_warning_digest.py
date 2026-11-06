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

from nyxboard.models import CheckNotificationState, HealthCheck, Result
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
    since_by_check = dict(
        CheckNotificationState.objects.values_list(
            "health_check_id", "first_failure_at"
        )
    )
    warnings = []
    for check in HealthCheck.objects.filter(disabled=False).order_by("name"):
        result = Result.objects.filter(health_check=check).order_by("-id").first()
        if result is None or result.status != ResultStatus.WARNING:
            continue
        data = result.data if isinstance(result.data, dict) else {}
        if any(data.get(key) for key in INERT_RESULT_KEYS):
            continue
        first_failure_at = since_by_check.get(check.pk)
        epoch = usable_epoch(first_failure_at)
        if epoch is None and first_failure_at not in (None, 0):
            logger.warning("Invalid first_failure_at for check %s", check.pk)
        since = (
            datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d")
            if epoch is not None
            else "unknown"
        )
        warnings.append(
            OpenWarning(
                check.name, since, str(data.get("error_msg", ""))[:MAX_ERROR_CHARS]
            )
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
