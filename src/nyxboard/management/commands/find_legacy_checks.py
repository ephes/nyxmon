"""
Management command to find and optionally disable legacy check types.

Legacy check types include:
- 'custom' (ssh-json CustomExecutor - removed for security reasons)
- Any other check_type not in the supported list

Usage:
    # List all legacy checks (dry run)
    python manage.py find_legacy_checks

    # Disable all legacy checks
    python manage.py find_legacy_checks --disable

    # Delete all legacy checks (with confirmation)
    python manage.py find_legacy_checks --delete
"""

import json
import tempfile
from itertools import islice

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connections, router, transaction
from django.db.models import F

from nyxboard.forms import DEFINITION_FIELDS, check_definition_revision
from nyxboard.models import HealthCheck
from nyxmon.adapters.notification import (
    UNTRUSTED_VALUE_MAX_CHARS,
    sanitize_untrusted_text,
)
from nyxmon.domain import CheckType


# Supported check types (all others are considered legacy)
SUPPORTED_CHECK_TYPES = [
    CheckType.HTTP,
    CheckType.JSON_HTTP,
    CheckType.TCP,
    CheckType.PING,
    CheckType.DNS,
    CheckType.SMTP,
    CheckType.IMAP,
    CheckType.JSON_METRICS,
]


class Command(BaseCommand):
    help = (
        "Find and optionally disable health checks with legacy/unsupported check types"
    )

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--disable",
            action="store_true",
            help="Disable all found legacy checks (default: list only)",
        )
        group.add_argument(
            "--delete",
            action="store_true",
            help="Delete all found legacy checks (use with caution)",
        )

    def handle(self, *args, **options):
        candidates = HealthCheck.objects.exclude(check_type__in=SUPPORTED_CHECK_TYPES)
        write_db = None
        if options["delete"] or options["disable"]:
            write_db = connections[router.db_for_write(HealthCheck)]
            if not write_db.get_autocommit():
                raise CommandError(
                    "Legacy cleanup requires autocommit on the write database. "
                    "Run --disable or --delete outside any transaction."
                )
            candidates = candidates.using(write_db.alias)
        horizon = candidates.order_by("-pk").values_list("pk", flat=True).first()
        if horizon is None:
            self._report_no_candidates()
            return
        # Anonymous disk-backed receipt stores only IDs and opaque revisions;
        # no full fleet or credentials remain in application memory.
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as receipt:
            count = enabled = cursor = 0
            while True:
                batch = list(
                    candidates.filter(pk__gt=cursor, pk__lte=horizon)
                    .select_related("service")
                    .only("pk", *DEFINITION_FIELDS, "service__name")
                    .order_by("pk")[:100]
                )
                if not batch:
                    break
                for check in batch:
                    receipt.write(
                        json.dumps([check.pk, check_definition_revision(check)]) + "\n"
                    )
                    count += 1
                    enabled += not check.disabled
                    status = "DISABLED" if check.disabled else "ENABLED"
                    name = sanitize_untrusted_text(
                        check.name, UNTRUSTED_VALUE_MAX_CHARS
                    )
                    check_type = sanitize_untrusted_text(
                        check.check_type, UNTRUSTED_VALUE_MAX_CHARS
                    )
                    service_name = sanitize_untrusted_text(
                        check.service.name, UNTRUSTED_VALUE_MAX_CHARS
                    )
                    self.stdout.write(
                        f"  - ID: {check.id}, Name: {name}, Type: {check_type}, "
                        f"Service: {service_name}, Status: {status}"
                    )
                cursor = batch[-1].pk
            if not count:
                self._report_no_candidates()
                return
            self.stdout.write(
                self.style.WARNING(
                    f"Found {count} check(s) with legacy/unsupported types."
                )
            )
            if options["delete"]:
                confirm = input(
                    f"\nAre you sure you want to DELETE {count} legacy check(s)? "
                    "This cannot be undone. [y/N]: "
                )
                if confirm.lower() != "y":
                    self.stdout.write("Delete operation cancelled.")
                    return
            if write_db is None:
                self.stdout.write(
                    f"\n{enabled} of these checks are currently ENABLED and will "
                    "cause ERROR results when the agent runs."
                )
                self.stdout.write(
                    "Run with --disable to disable them, or --delete to remove them."
                )
                return
            receipt.seek(0)
            # Leave room for assignment/filter parameters on the backend.
            batch_size = min(
                100, max(1, (write_db.features.max_query_params or 102) - 2)
            )
            completed = skipped = already = processed = 0
            try:
                while lines := list(islice(receipt, batch_size)):
                    revisions = dict(json.loads(line) for line in lines)
                    changed, stale, disabled = self._mutate_batch(
                        revisions, options["delete"], write_db.alias
                    )
                    completed += changed
                    skipped += stale
                    already += disabled
                    processed += len(revisions)
                    self.stdout.write(
                        f"Progress: completed={completed}, skipped={skipped}, "
                        f"already_disabled={already}, remaining={count - processed}"
                    )
            except (DatabaseError, OSError) as exc:
                raise CommandError(
                    f"Cleanup stopped: completed={completed}, skipped={skipped}, "
                    f"already_disabled={already}, remaining={count - processed}. "
                    "Earlier batches are committed; rerun inventory before retrying."
                ) from exc
            verb = "Deleted" if options["delete"] else "Disabled"
            self.stdout.write(
                self.style.SUCCESS(
                    f"{verb} {completed} legacy check(s). Skipped {skipped} changed/deleted; "
                    f"{already} already disabled."
                )
            )

    def _report_no_candidates(self):
        self.stdout.write(
            self.style.SUCCESS(
                "No legacy check types found. All checks are using supported types."
            )
        )

    def _mutate_batch(self, revisions, delete, using):
        changed = already = 0
        checks = HealthCheck.objects.using(using)
        with transaction.atomic(using=using, durable=True):
            batch = checks.filter(pk__in=revisions)
            if connections[using].vendor == "sqlite":
                # Reserve SQLite's writer before re-reading definitions.
                batch.update(disabled=F("disabled"))
            current = batch.order_by("pk").select_for_update()
            for check in current:
                if (
                    check.check_type in SUPPORTED_CHECK_TYPES
                    or check_definition_revision(check) != revisions[check.pk]
                ):
                    continue
                if delete:
                    check.delete(using=using)
                    changed += 1
                elif not check.disabled:
                    checks.filter(pk=check.pk).update(disabled=True)
                    changed += 1
                else:
                    already += 1
        return changed, len(revisions) - changed - already, already
