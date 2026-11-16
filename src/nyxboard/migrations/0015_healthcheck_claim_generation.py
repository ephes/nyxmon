"""Django owns claim generations; apply before adopting the updated Rust agent."""

import uuid

from django.db import migrations, models


def initialize_processing(apps, schema_editor):
    checks = apps.get_model("nyxboard", "HealthCheck")
    for check in (
        checks.objects.using(schema_editor.connection.alias)
        .filter(status="processing")
        .iterator()
    ):
        check.claim_generation = uuid.uuid4().hex
        check.save(
            using=schema_editor.connection.alias, update_fields=["claim_generation"]
        )


class Migration(migrations.Migration):
    dependencies = [("nyxboard", "0014_login_throttle_bucket")]
    operations = [
        migrations.AddField(
            model_name="healthcheck",
            name="claim_generation",
            field=models.CharField(
                max_length=32,
                default="",
                db_default="",
                blank=True,
                editable=False,
                help_text="Opaque generation of the last claim; timestamps only control expiry",
            ),
        ),
        migrations.RunPython(initialize_processing, migrations.RunPython.noop),
    ]
