"""Record the current check-type and result-status choices.

The ``custom`` check type (SSH-JSON) was removed from the model and its executor
deleted without a migration, and ``Result.status`` gained ``warning``. Choices
are not database constraints, so the ``AlterField`` operations change no schema.

An *enabled* health check whose type is not one of the choices recorded here
(``custom`` or any other legacy type) can only produce ERROR results, so this
migration refuses to apply while such checks exist and names them. Disabled
legacy checks stay supported (the dashboard can still edit them) and do not
block it. The operator resolves the refusal with
``manage.py find_legacy_checks --disable`` (or ``--delete``), which needs no
schema change. The guard never changes rows and reversing it is a no-op.
"""

from django.db import migrations, models

#: The check types recorded by this migration (``CHECK_TYPE_CHOICES``).
SUPPORTED_CHECK_TYPES = (
    "http",
    "json-http",
    "tcp",
    "ping",
    "dns",
    "smtp",
    "imap",
    "json-metrics",
)

#: How many blocking checks the refusal message names individually.
MAX_LISTED = 20


def refuse_enabled_legacy_checks(apps, schema_editor):
    checks = apps.get_model("nyxboard", "HealthCheck")
    blocking = (
        checks.objects.using(schema_editor.connection.alias)
        .exclude(check_type__in=SUPPORTED_CHECK_TYPES)
        .filter(disabled=False)
    )
    count = blocking.count()
    if not count:
        return
    listed = ", ".join(
        f"#{pk} {name!r} (type {check_type!r})"
        for pk, name, check_type in blocking.order_by("pk").values_list(
            "pk", "name", "check_type"
        )[:MAX_LISTED]
    )
    more = f" and {count - MAX_LISTED} more" if count > MAX_LISTED else ""
    raise RuntimeError(
        f"{count} enabled health check(s) use a removed check type such as "
        f"'custom' ({listed}{more}); they can only produce ERROR results. "
        "Disable them with `manage.py find_legacy_checks --disable` (or remove "
        "them with `--delete`), then run the migration again. Disabled legacy "
        "checks are kept and do not block this migration."
    )


class Migration(migrations.Migration):
    dependencies = [
        ("nyxboard", "0015_healthcheck_claim_generation"),
    ]

    operations = [
        migrations.RunPython(refuse_enabled_legacy_checks, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="healthcheck",
            name="check_type",
            field=models.CharField(
                choices=[
                    ("http", "HTTP"),
                    ("json-http", "JSON-HTTP"),
                    ("tcp", "TCP"),
                    ("ping", "Ping"),
                    ("dns", "DNS"),
                    ("smtp", "SMTP"),
                    ("imap", "IMAP"),
                    ("json-metrics", "JSON Metrics"),
                ],
                default="http",
                max_length=20,
                verbose_name="Check Type",
            ),
        ),
        migrations.AlterField(
            model_name="result",
            name="status",
            field=models.CharField(
                choices=[("ok", "OK"), ("warning", "Warning"), ("error", "Error")],
                max_length=10,
                verbose_name="Status",
            ),
        ),
    ]
