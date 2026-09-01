"""Elapsed-time reminder state plus the persisted collector-incident table.

Follows the ``SeparateDatabaseAndState`` pattern established by
``0011_checknotificationstate``: the worker (``SqliteCheckRepository._ensure_schema``)
creates and upgrades these tables independently, so the database side must stay
idempotent while Django's model state is updated exactly once.

The two new ``check_notification_state`` columns are added with ``RunPython``
rather than ``RunSQL`` because SQLite has no ``ADD COLUMN IF NOT EXISTS`` and the
worker may legitimately have added them already.

Bootstrap semantics (deliberate, see the notification runbook): a check that was
already failing *and* had already alerted at least once is adopted as an ongoing
incident - ``last_notified_at`` is stamped with the migration time so its next
reminder is one full reminder window away. It is NOT re-paged as a new incident.
A check that was failing but had never reached the alert threshold keeps
``last_notified_at = 0`` and follows the normal initial-alert threshold.
"""

from django.db import migrations, models


NEW_COLUMNS = {
    "last_notified_at": "INTEGER NOT NULL DEFAULT 0",
    "first_failure_at": "INTEGER NOT NULL DEFAULT 0",
}


def add_reminder_columns(apps, schema_editor):
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA table_info(check_notification_state)")
        existing = {row[1] for row in cursor.fetchall()}
        added = []
        for column, definition in NEW_COLUMNS.items():
            if column in existing:
                continue
            cursor.execute(
                f"ALTER TABLE check_notification_state ADD COLUMN {column} {definition}"
            )
            added.append(column)
        if not added:
            return
        if "first_failure_at" in added:
            cursor.execute(
                "UPDATE check_notification_state "
                "SET first_failure_at = CAST(strftime('%s', 'now') AS INTEGER) "
                "WHERE failure_count > 0 AND first_failure_at = 0"
            )
        if "last_notified_at" in added:
            cursor.execute(
                "UPDATE check_notification_state "
                "SET last_notified_at = CAST(strftime('%s', 'now') AS INTEGER) "
                "WHERE failure_count > 0 AND last_attempt_count > 0 "
                "AND last_notified_at = 0"
            )


def drop_reminder_columns(apps, schema_editor):
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA table_info(check_notification_state)")
        existing = {row[1] for row in cursor.fetchall()}
        for column in NEW_COLUMNS:
            if column in existing:
                cursor.execute(
                    f"ALTER TABLE check_notification_state DROP COLUMN {column}"
                )


class Migration(migrations.Migration):
    dependencies = [
        ("nyxboard", "0011_checknotificationstate"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(
                    add_reminder_columns,
                    drop_reminder_columns,
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="checknotificationstate",
                    name="last_notified_at",
                    field=models.PositiveBigIntegerField(
                        default=0,
                        help_text=(
                            "Unix timestamp of the last external notification for "
                            "the current incident; 0 means the incident has never "
                            "alerted"
                        ),
                    ),
                ),
                migrations.AddField(
                    model_name="checknotificationstate",
                    name="first_failure_at",
                    field=models.PositiveBigIntegerField(
                        default=0,
                        help_text=(
                            "Unix timestamp of the first failing sample in the "
                            "current incident"
                        ),
                    ),
                ),
            ],
        ),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql="""
                        CREATE TABLE IF NOT EXISTS collector_incident (
                            incident_key  TEXT    PRIMARY KEY,
                            opened_at     INTEGER NOT NULL DEFAULT 0,
                            last_alert_at INTEGER NOT NULL DEFAULT 0,
                            alert_count   INTEGER NOT NULL DEFAULT 0,
                            payload       TEXT    NOT NULL DEFAULT '{}'
                        )
                    """,
                    reverse_sql="DROP TABLE IF EXISTS collector_incident",
                )
            ],
            state_operations=[
                migrations.CreateModel(
                    name="CollectorIncident",
                    fields=[
                        (
                            "incident_key",
                            models.CharField(
                                max_length=200, primary_key=True, serialize=False
                            ),
                        ),
                        ("opened_at", models.PositiveBigIntegerField(default=0)),
                        ("last_alert_at", models.PositiveBigIntegerField(default=0)),
                        ("alert_count", models.PositiveIntegerField(default=0)),
                        ("payload", models.JSONField(blank=True, default=dict)),
                    ],
                    options={"db_table": "collector_incident"},
                )
            ],
        ),
    ]
