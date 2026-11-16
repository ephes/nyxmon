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

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F

from nyxboard.forms import check_definition_revision

from nyxboard.models import HealthCheck
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
        # Find all checks with unsupported types using ORM filter
        legacy_checks = list(
            HealthCheck.objects.exclude(
                check_type__in=SUPPORTED_CHECK_TYPES
            ).select_related("service")
        )

        if not legacy_checks:
            self.stdout.write(
                self.style.SUCCESS(
                    "No legacy check types found. All checks are using supported types."
                )
            )
            return

        # Report what was found
        self.stdout.write(
            self.style.WARNING(
                f"Found {len(legacy_checks)} check(s) with legacy/unsupported types:"
            )
        )

        revisions = {check.pk: check_definition_revision(check) for check in legacy_checks}
        for check in legacy_checks:
            status = "DISABLED" if check.disabled else "ENABLED"
            self.stdout.write(
                f"  - ID: {check.id}, Name: {check.name}, "
                f"Type: {check.check_type}, Service: {check.service.name}, "
                f"Status: {status}"
            )

        if options["delete"]:
            confirm = input(
                f"\nAre you sure you want to DELETE {len(legacy_checks)} legacy check(s)? "
                "This cannot be undone. [y/N]: "
            )
            if confirm.lower() != "y":
                self.stdout.write("Delete operation cancelled.")
                return
        if options["delete"] or options["disable"]:
            changed, skipped, already = self._mutate_batch(revisions, options["delete"])
            verb = "Deleted" if options["delete"] else "Disabled"
            self.stdout.write(self.style.SUCCESS(
                f"{verb} {changed} legacy check(s). Skipped {skipped} changed/deleted; "
                f"{already} already disabled."
            ))
        else:
            enabled_count = sum(not check.disabled for check in legacy_checks)
            self.stdout.write(
                f"\n{enabled_count} of these checks are currently ENABLED and will "
                "cause ERROR results when the agent runs."
            )
            self.stdout.write("Run with --disable to disable them, or --delete to remove them.")

    def _mutate_batch(self, revisions, delete):
        changed = already = 0
        with transaction.atomic():
            # Reserve SQLite's writer before re-reading definitions. On other
            # backends the following row locks serialize concurrent edits.
            HealthCheck.objects.filter(pk__in=revisions).update(disabled=F("disabled"))
            current = HealthCheck.objects.filter(pk__in=revisions).select_for_update()
            for check in current:
                if check.check_type in SUPPORTED_CHECK_TYPES or check_definition_revision(check) != revisions[check.pk]:
                    continue
                if delete:
                    check.delete()
                    changed += 1
                elif not check.disabled:
                    HealthCheck.objects.filter(pk=check.pk).update(disabled=True)
                    changed += 1
                else:
                    already += 1
        return changed, len(revisions) - changed - already, already
