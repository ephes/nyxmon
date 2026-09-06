"""Upgrade path for the notification state schema (0011 -> 0012 -> 0013).

Locks in the bootstrap contract: rolling out elapsed-time reminders must not
turn every already-failing check into a new incident. The 0013 cases lock in
the other half of the contract: the worker and ``manage.py migrate`` add the
site-connectivity columns in either order without duplicating or losing one.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.utils import OperationalError
from django.db.migrations.executor import MigrationExecutor

MIGRATE_FROM = ("nyxboard", "0011_checknotificationstate")
MIGRATE_TO = ("nyxboard", "0012_notification_reminder_timestamps")
MIGRATE_SITE_CONNECTIVITY = ("nyxboard", "0013_site_connectivity_state")

STATE_COLUMNS_0013 = [
    "check_id",
    "failure_count",
    "last_attempt_count",
    "last_immediate_at",
    "last_notified_at",
    "first_failure_at",
    "held_since",
    "attempt_seq",
    "attempt_at",
]
SITE_CONNECTIVITY_COLUMNS = ("held_since", "attempt_seq", "attempt_at")


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
    _migrate([MIGRATE_SITE_CONNECTIVITY])


def _state_columns():
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA table_info(check_notification_state)")
        return [row[1] for row in cursor.fetchall()]


def _add_columns_like_the_worker() -> list[str]:
    """Mimic ``SqliteCheckRepository._upgrade_notification_state_schema``.

    Same statements, same swallowing of ``duplicate column name``.

    Returns:
        The columns this pass actually added.
    """
    added: list[str] = []
    with connection.cursor() as cursor:
        for column in SITE_CONNECTIVITY_COLUMNS:
            try:
                cursor.execute(
                    "ALTER TABLE check_notification_state "
                    f"ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                )
            except OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
            else:
                added.append(column)
    return added


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


@pytest.mark.django_db(transaction=True)
def test_site_connectivity_columns_are_added_without_a_backfill(migrator) -> None:
    """0012 -> 0013 by migration alone, with an already-alerting streak present.

    All three columns default to 0, so an existing incident must come out of
    the migration exactly as it went in: unheld, with no delivery pending.
    """
    _migrate([MIGRATE_TO])
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO service (name) VALUES ('svc')")
        service_id = cursor.execute("SELECT id FROM service LIMIT 1").fetchone()[0]
        cursor.execute(
            """INSERT INTO health_check
                   (id, service_id, name, check_type, url, check_interval,
                    status, next_check_time, processing_started_at, disabled, data)
               VALUES (1, ?, 'alerting', 'http', 'https://example.test/x', 3600,
                       'idle', 0, 0, 0, '{}')""",
            [service_id],
        )
        cursor.execute(
            """INSERT INTO check_notification_state
                   (check_id, failure_count, last_attempt_count, last_immediate_at,
                    last_notified_at, first_failure_at)
               VALUES (1, 5, 5, 0, 900, 800)"""
        )
    assert "held_since" not in _state_columns()

    _migrate([MIGRATE_SITE_CONNECTIVITY])

    assert _state_columns() == STATE_COLUMNS_0013
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT failure_count, last_notified_at, first_failure_at, "
            "held_since, attempt_seq, attempt_at FROM check_notification_state"
        )
        assert cursor.fetchall() == [(5, 900, 800, 0, 0, 0)]


@pytest.mark.django_db(transaction=True)
def test_migration_0013_after_the_worker_already_upgraded(migrator) -> None:
    """Worker first, then ``manage.py migrate``: no duplicate columns."""
    _migrate([MIGRATE_TO])
    assert _add_columns_like_the_worker() == list(SITE_CONNECTIVITY_COLUMNS)

    _migrate([MIGRATE_SITE_CONNECTIVITY])

    assert _state_columns() == STATE_COLUMNS_0013


@pytest.mark.django_db(transaction=True)
def test_worker_upgrade_after_migration_0013_is_a_no_op(migrator) -> None:
    """Migration first, then the worker: its ALTER TABLEs are already satisfied.

    The worker swallows ``duplicate column name``; this pins that the migration
    leaves the table in exactly the shape the worker expects, so a rollout in
    this order needs no second pass.
    """
    _migrate([MIGRATE_SITE_CONNECTIVITY])
    assert _state_columns() == STATE_COLUMNS_0013

    assert _add_columns_like_the_worker() == []

    assert _state_columns() == STATE_COLUMNS_0013


@pytest.mark.django_db(transaction=True)
def test_migration_0013_reverses_cleanly(migrator) -> None:
    _migrate([MIGRATE_SITE_CONNECTIVITY])
    assert _state_columns() == STATE_COLUMNS_0013

    _migrate([MIGRATE_TO])

    assert _state_columns() == STATE_COLUMNS_0013[:6]
