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

        for check in legacy_checks:
            status = "DISABLED" if check.disabled else "ENABLED"
            self.stdout.write(
                f"  - ID: {check.id}, Name: {check.name}, "
                f"Type: {check.check_type}, Service: {check.service.name}, "
                f"Status: {status}"
            )

        # Handle --delete option
        if options["delete"]:
            # Get confirmation BEFORE starting the transaction
            confirm = input(
                f"\nAre you sure you want to DELETE {len(legacy_checks)} legacy check(s)? "
                "This cannot be undone. [y/N]: "
            )
            if confirm.lower() == "y":
                # Wrap only the write operation in a transaction
                with transaction.atomic():
                    check_ids = [check.id for check in legacy_checks]
                    deleted_count, _ = HealthCheck.objects.filter(
                        id__in=check_ids
                    ).delete()
                self.stdout.write(
                    self.style.SUCCESS(f"Deleted {deleted_count} legacy check(s).")
                )
            else:
                self.stdout.write("Delete operation cancelled.")
            return

        # Handle --disable option
        if options["disable"]:
            # Wrap only the write operation in a transaction
            with transaction.atomic():
                checks_to_disable = [
                    check for check in legacy_checks if not check.disabled
                ]
                if checks_to_disable:
                    check_ids = [check.id for check in checks_to_disable]
                    disabled_count = HealthCheck.objects.filter(
                        id__in=check_ids
                    ).update(disabled=True)
                else:
                    disabled_count = 0

            if disabled_count > 0:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Disabled {disabled_count} legacy check(s). "
                        f"({len(legacy_checks) - disabled_count} were already disabled)"
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS("All legacy checks were already disabled.")
                )
        else:
            # Dry run - just list
            enabled_count = sum(1 for check in legacy_checks if not check.disabled)
            self.stdout.write(
                f"\n{enabled_count} of these checks are currently ENABLED and will "
                "cause ERROR results when the agent runs."
            )
            self.stdout.write(
                "Run with --disable to disable them, or --delete to remove them."
            )
