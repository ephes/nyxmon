"""Site-connectivity hold state and per-check delivery intent.

Follows the ``SeparateDatabaseAndState`` pattern of
``0012_notification_reminder_timestamps``: the worker
(``SqliteCheckRepository._ensure_schema``) upgrades this table independently,
so the database side stays idempotent while Django's model state is updated
exactly once. Either order is safe - ``manage.py migrate`` first or the worker
first - because both add each column only when ``PRAGMA table_info`` says it is
missing.

Unlike 0012 there is no backfill: all three columns are meaningful at their
zero default. ``held_since = 0`` means "not held", ``attempt_seq = 0`` starts
the monotonic attempt counter, and ``attempt_at = 0`` means "no delivery is
pending", which is exactly the state of every check before the feature exists.
"""

from django.db import migrations, models


NEW_COLUMNS = {
    "held_since": "INTEGER NOT NULL DEFAULT 0",
    "attempt_seq": "INTEGER NOT NULL DEFAULT 0",
    "attempt_at": "INTEGER NOT NULL DEFAULT 0",
}


def add_site_connectivity_columns(apps, schema_editor):
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA table_info(check_notification_state)")
        existing = {row[1] for row in cursor.fetchall()}
        for column, definition in NEW_COLUMNS.items():
            if column in existing:
                continue
            cursor.execute(
                f"ALTER TABLE check_notification_state ADD COLUMN {column} {definition}"
            )


def drop_site_connectivity_columns(apps, schema_editor):
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
        ("nyxboard", "0012_notification_reminder_timestamps"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(
                    add_site_connectivity_columns,
                    drop_site_connectivity_columns,
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="checknotificationstate",
                    name="held_since",
                    field=models.PositiveBigIntegerField(
                        default=0,
                        help_text=(
                            "Unix timestamp of the first sample held back because "
                            "a site dependency of this check is down; 0 when the "
                            "check is not held"
                        ),
                    ),
                ),
                migrations.AddField(
                    model_name="checknotificationstate",
                    name="attempt_seq",
                    field=models.PositiveBigIntegerField(
                        default=0,
                        help_text=(
                            "Monotonic counter of external notification attempts; "
                            "never reset, it fences the acknowledgement written "
                            "after a send"
                        ),
                    ),
                ),
                migrations.AddField(
                    model_name="checknotificationstate",
                    name="attempt_at",
                    field=models.PositiveBigIntegerField(
                        default=0,
                        help_text=(
                            "Unix timestamp of the current unacknowledged "
                            "notification attempt; 0 when no delivery is pending"
                        ),
                    ),
                ),
            ],
        ),
    ]
