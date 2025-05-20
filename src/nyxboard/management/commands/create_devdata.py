"""
Management command to create development data for testing.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from nyxboard.devdata import create_sample_data


class Command(BaseCommand):
    help = "Creates sample development data (services and checks) for testing"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Force creation even if data already exists",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        from nyxboard.models import Service, HealthCheck

        # Check if data already exists
        existing_services = Service.objects.count()
        existing_checks = HealthCheck.objects.count()

        if existing_services > 0 or existing_checks > 0:
            if not options["force"]:
                self.stdout.write(
                    self.style.WARNING(
                        f"Data already exists: {existing_services} services, "
                        f"{existing_checks} checks. Use --force to override."
                    )
                )
                return
            self.stdout.write(
                self.style.WARNING(
                    f"Proceeding with data creation despite existing data: "
                    f"{existing_services} services, {existing_checks} checks."
                )
            )

        # Create the sample data
        try:
            created = create_sample_data()

            # Report what was created
            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully created {len(created['services'])} services "
                    f"and {len(created['checks'])} checks:"
                )
            )

            for service in created["services"]:
                self.stdout.write(f"  - Service: {service.name} (ID: {service.id})")

            for check in created["checks"]:
                self.stdout.write(
                    f"  - Check: {check.name} (ID: {check.id}) "
                    f"for {check.service.name} - URL: {check.url}"
                )

            self.stdout.write(
                self.style.SUCCESS(
                    "You can now run the monitoring agent to start checking these endpoints"
                )
            )

        except Exception as e:
            raise CommandError(f"Error creating development data: {e}")
