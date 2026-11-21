"""
Management command to create development data for testing.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import router, transaction

from nyxboard.devdata import create_sample_data
from nyxboard.models import HealthCheck, Service


class Command(BaseCommand):
    help = "Creates sample development data (services and checks) for testing"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Force creation even if data already exists",
        )

    def handle(self, *args, **options):
        using = router.db_for_write(Service)
        check_writer = router.db_for_write(HealthCheck)
        if using != check_writer:
            raise CommandError(
                "Development fixtures require the same write database for services "
                f"({using}) and health checks ({check_writer})."
            )

        try:
            with transaction.atomic(using=using):
                existing_services = Service.objects.using(using).count()
                existing_checks = HealthCheck.objects.using(using).count()
                has_data = bool(existing_services or existing_checks)
                created = None
                if options["force"] or not has_data:
                    created = create_sample_data(using=using)
        except Exception as e:
            raise CommandError(f"Error creating development data: {e}") from e

        counts = f"{existing_services} services, {existing_checks} checks"
        if created is None:
            self.stdout.write(
                self.style.WARNING(
                    f"Data already exists: {counts}. Use --force to override."
                )
            )
            return

        if has_data:
            self.stdout.write(
                self.style.WARNING(
                    f"Proceeding with data creation despite existing data: {counts}."
                )
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully created {len(created['services'])} services "
                f"and {len(created['checks'])} checks:"
            )
        )
        service_names = {service.pk: service.name for service in created["services"]}
        for service in created["services"]:
            self.stdout.write(f"  - Service: {service.name} (ID: {service.id})")

        for check in created["checks"]:
            self.stdout.write(
                f"  - Check: {check.name} (ID: {check.id}) "
                f"for {service_names[check.service_id]} - URL: {check.url}"
            )

        self.stdout.write(
            self.style.SUCCESS(
                "You can now run the monitoring agent to start checking these endpoints"
            )
        )
