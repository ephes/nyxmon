"""Migration 0016 records the current choices and refuses enabled legacy checks.

The ``custom`` check type was removed from the model (and its executor deleted)
without a migration. An enabled check of a removed type only produces ERROR
results, so 0016 aborts while one exists and points at ``find_legacy_checks``;
disabled legacy checks remain a supported state and do not block it.
"""

from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = ("nyxboard", "0015_healthcheck_claim_generation")
TARGET = ("nyxboard", "0016_alter_healthcheck_check_type_alter_result_status")


def _graph_leaves():
    return MigrationExecutor(connection).loader.graph.leaf_nodes()


def _restore(latest):
    """Migrate back to the leaf even when a test left blocking checks behind."""
    with connection.cursor() as cursor:
        cursor.execute("UPDATE health_check SET disabled = %s", [True])
    MigrationExecutor(connection).migrate(latest)


@pytest.mark.django_db
def test_models_and_migrations_are_in_sync():
    """``makemigrations --check`` must find nothing to write (drift gate)."""
    out = StringIO()
    try:
        call_command("makemigrations", check=True, dry_run=True, stdout=out, stderr=out)
    except SystemExit as exc:  # pragma: no cover - failure path
        pytest.fail(
            "Models and migrations have drifted; create the missing migration "
            f"(exit {exc.code}):\n{out.getvalue()}"
        )


@pytest.mark.django_db(transaction=True)
def test_migration_applies_without_custom_checks():
    latest = _graph_leaves()
    try:
        executor = MigrationExecutor(connection)
        executor.migrate([BEFORE])
        apps = executor.loader.project_state([BEFORE]).apps
        service = apps.get_model("nyxboard", "Service").objects.create(name="synthetic")
        apps.get_model("nyxboard", "HealthCheck").objects.create(
            service=service, name="synthetic http", check_type="http"
        )
        executor = MigrationExecutor(connection)
        executor.migrate([TARGET])
        executor = MigrationExecutor(connection)
        assert TARGET in executor.loader.applied_migrations
        checks = executor.loader.project_state([TARGET]).apps.get_model(
            "nyxboard", "HealthCheck"
        )
        assert list(checks.objects.values_list("check_type", flat=True)) == ["http"]
    finally:
        _restore(latest)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("legacy_type", ["custom", "ssh-json"])
def test_migration_refuses_enabled_legacy_checks_and_keeps_them(legacy_type):
    latest = _graph_leaves()
    try:
        executor = MigrationExecutor(connection)
        executor.migrate([BEFORE])
        apps = executor.loader.project_state([BEFORE]).apps
        service = apps.get_model("nyxboard", "Service").objects.create(name="synthetic")
        checks = apps.get_model("nyxboard", "HealthCheck")
        checks.objects.create(service=service, name="synthetic http", check_type="http")
        legacy = checks.objects.create(
            service=service, name="synthetic legacy", check_type=legacy_type
        )

        executor = MigrationExecutor(connection)
        with pytest.raises(RuntimeError) as excinfo:
            executor.migrate([TARGET])
        message = str(excinfo.value)
        assert message.startswith("1 enabled health check(s)")
        assert f"#{legacy.pk} 'synthetic legacy' (type '{legacy_type}')" in message
        assert "find_legacy_checks --disable" in message

        executor = MigrationExecutor(connection)
        assert TARGET not in executor.loader.applied_migrations
        assert BEFORE in executor.loader.applied_migrations
        rows = executor.loader.project_state([BEFORE]).apps.get_model(
            "nyxboard", "HealthCheck"
        )
        assert sorted(rows.objects.values_list("check_type", "disabled")) == sorted(
            [("http", False), (legacy_type, False)]
        )

        # Disabling the leftover check (what find_legacy_checks --disable does)
        # unblocks the migration and keeps the row.
        rows.objects.filter(pk=legacy.pk).update(disabled=True)
        executor.migrate([TARGET])
        executor = MigrationExecutor(connection)
        assert TARGET in executor.loader.applied_migrations
        rows = executor.loader.project_state([TARGET]).apps.get_model(
            "nyxboard", "HealthCheck"
        )
        assert rows.objects.get(pk=legacy.pk).check_type == legacy_type
    finally:
        _restore(latest)


@pytest.mark.django_db(transaction=True)
def test_refusal_lists_at_most_twenty_checks():
    latest = _graph_leaves()
    try:
        executor = MigrationExecutor(connection)
        executor.migrate([BEFORE])
        apps = executor.loader.project_state([BEFORE]).apps
        service = apps.get_model("nyxboard", "Service").objects.create(name="synthetic")
        checks = apps.get_model("nyxboard", "HealthCheck")
        for i in range(23):
            checks.objects.create(
                service=service, name=f"synthetic {i}", check_type="custom"
            )
        executor = MigrationExecutor(connection)
        with pytest.raises(RuntimeError) as excinfo:
            executor.migrate([TARGET])
        message = str(excinfo.value)
        assert message.startswith("23 enabled health check(s)")
        assert message.count("(type 'custom')") == 20
        assert "and 3 more" in message
    finally:
        _restore(latest)


@pytest.mark.django_db(transaction=True)
def test_migration_reverses_cleanly():
    latest = _graph_leaves()
    try:
        executor = MigrationExecutor(connection)
        executor.migrate([TARGET])
        executor = MigrationExecutor(connection)
        executor.migrate([BEFORE])
        assert TARGET not in MigrationExecutor(connection).loader.applied_migrations
    finally:
        _restore(latest)
