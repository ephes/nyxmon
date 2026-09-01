"""Upgrade path for the notification state schema (0011 -> 0012).

Locks in the bootstrap contract: rolling out elapsed-time reminders must not
turn every already-failing check into a new incident.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

MIGRATE_FROM = ("nyxboard", "0011_checknotificationstate")
MIGRATE_TO = ("nyxboard", "0012_notification_reminder_timestamps")


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    executor.loader.build_graph()
    return executor


@pytest.fixture
def migrator():
    yield
    # Always leave the test database fully migrated for the rest of the session.
    _migrate([MIGRATE_TO])


@pytest.mark.django_db(transaction=True)
def test_existing_failure_streaks_are_adopted_not_re_paged(migrator) -> None:
    _migrate([MIGRATE_FROM])

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO service (name) VALUES ('svc')")
        cursor.execute("SELECT id FROM service LIMIT 1")
        service_id = cursor.fetchone()[0]
        for check_id, name in (
            (1, "already alerting"),
            (2, "healthy"),
            (3, "failing below threshold"),
        ):
            cursor.execute(
                """INSERT INTO health_check
                       (id, service_id, name, check_type, url, check_interval,
                        status, next_check_time, processing_started_at, disabled,
                        data)
                   VALUES (?, ?, ?, 'http', 'https://example.test/x', 3600,
                           'idle', 0, 0, 0, '{}')""",
                [check_id, service_id, name],
            )
        cursor.executemany(
            """INSERT INTO check_notification_state
                   (check_id, failure_count, last_attempt_count, last_immediate_at)
               VALUES (?, ?, ?, ?)""",
            [(1, 121, 121, 0), (2, 0, 0, 0), (3, 1, 0, 0)],
        )
        cursor.execute("PRAGMA table_info(check_notification_state)")
        assert "last_notified_at" not in {row[1] for row in cursor.fetchall()}

    _migrate([MIGRATE_TO])

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT check_id, failure_count, last_attempt_count, "
            "last_notified_at, first_failure_at "
            "FROM check_notification_state ORDER BY check_id"
        )
        rows = {row[0]: row[1:] for row in cursor.fetchall()}

    established = rows[1]
    assert established[0] == 121 and established[1] == 121
    # Adopted as an ongoing incident: stamped as notified, so no new page.
    assert established[2] > 0
    assert established[3] > 0

    # A healthy check stays pristine.
    assert rows[2] == (0, 0, 0, 0)

    # A streak that never alerted keeps last_notified_at == 0 so the normal
    # initial-alert threshold still applies.
    assert rows[3][0] == 1
    assert rows[3][2] == 0
    assert rows[3][3] > 0


@pytest.mark.django_db(transaction=True)
def test_migration_is_idempotent_against_a_worker_that_already_upgraded(
    migrator,
) -> None:
    _migrate([MIGRATE_FROM])
    with connection.cursor() as cursor:
        # Simulate the monitor worker's _ensure_schema having added the columns
        # before `manage.py migrate` ran.
        cursor.execute(
            "ALTER TABLE check_notification_state "
            "ADD COLUMN last_notified_at INTEGER NOT NULL DEFAULT 0"
        )
        cursor.execute(
            "ALTER TABLE check_notification_state "
            "ADD COLUMN first_failure_at INTEGER NOT NULL DEFAULT 0"
        )

    _migrate([MIGRATE_TO])

    with connection.cursor() as cursor:
        cursor.execute("PRAGMA table_info(check_notification_state)")
        columns = [row[1] for row in cursor.fetchall()]
        cursor.execute("PRAGMA table_info(collector_incident)")
        incident_columns = {row[1] for row in cursor.fetchall()}

    assert columns.count("last_notified_at") == 1
    assert columns.count("first_failure_at") == 1
    assert incident_columns == {
        "incident_key",
        "opened_at",
        "last_alert_at",
        "alert_count",
        "payload",
    }
