import sqlite3
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Event

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections

from nyxboard.models import HealthCheck, Service

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def seed_db(tmp_path, settings, django_db_blocker):
    path = tmp_path / "devdata.sqlite3"
    with sqlite3.connect(path) as copied:
        connection.ensure_connection()
        connection.connection.backup(copied)
        copied.execute("PRAGMA journal_mode=WAL")
    alias = "devdata_writer"
    connections.databases[alias] = {**connection.settings_dict, "NAME": str(path)}

    class SeedRouter:
        check_writer = alias

        def db_for_read(self, model, **hints):
            return "default"

        def db_for_write(self, model, **hints):
            if model is Service:
                return alias
            if model is HealthCheck:
                return self.check_writer

    router = SeedRouter()
    settings.DATABASE_ROUTERS = [router]
    try:
        with django_db_blocker.unblock():
            yield connections[alias], router
    finally:
        connections[alias].close()
        del connections[alias]
        del connections.databases[alias]


def test_devdata_rolls_back_both_models_on_the_writer(seed_db):
    writer, _ = seed_db
    checks = HealthCheck.objects.using(writer.alias)
    services = Service.objects.using(writer.alias)
    attempts = []

    def fail_second_check(execute, sql, params, many, context):
        if sql.startswith('INSERT INTO "health_check"'):
            attempts.append(params)
            if len(attempts) == 2:
                assert services.count() == checks.count() == 1
                raise RuntimeError("second check failed")
        return execute(sql, params, many, context)

    with (
        writer.execute_wrapper(fail_second_check),
        pytest.raises(CommandError, match="second check failed"),
    ):
        call_command("create_devdata", stdout=StringIO())
    assert len(attempts) == 2
    assert services.count() == checks.count() == 0
    assert Service.objects.using("default").count() == 0
    assert HealthCheck.objects.using("default").count() == 0


def test_devdata_uses_writer_counts_and_preserves_force(seed_db):
    writer, _ = seed_db
    services = Service.objects.using(writer.alias)
    checks = HealthCheck.objects.using(writer.alias)
    services.create(name="Writer only")
    output = StringIO()

    call_command("create_devdata", stdout=output)

    assert "Data already exists: 1 services, 0 checks" in output.getvalue()
    assert services.count() == 1
    assert checks.count() == 0

    call_command("create_devdata", force=True, stdout=output)

    assert "Successfully created 1 services and 2 checks" in output.getvalue()
    assert services.count() == 2
    assert checks.count() == 2
    assert Service.objects.using("default").count() == 0
    assert HealthCheck.objects.using("default").count() == 0


def test_devdata_refuses_split_writers_even_with_force(seed_db):
    writer, router = seed_db
    router.check_writer = "default"
    output = StringIO()

    with pytest.raises(CommandError, match="same write database"):
        call_command("create_devdata", force=True, stdout=output)

    assert not output.getvalue()
    for alias in ("default", writer.alias):
        assert Service.objects.using(alias).count() == 0
        assert HealthCheck.objects.using(alias).count() == 0


def test_concurrent_unforced_devdata_seeds_once(seed_db):
    writer, _ = seed_db
    alias = writer.alias
    first_at_insert = Event()
    second_at_write = Event()

    def pause_first(execute, sql, params, many, context):
        if sql.startswith('INSERT INTO "service"'):
            first_at_insert.set()
            assert second_at_write.wait(timeout=10), (
                "second command never tried to write"
            )
        return execute(sql, params, many, context)

    def release_first(execute, sql, params, many, context):
        if sql.startswith(("UPDATE ", "INSERT ")):
            second_at_write.set()
        return execute(sql, params, many, context)

    def run_command(wrapper):
        # Django gives each thread its own connection to the same SQLite file.
        db = connections[alias]
        output = StringIO()
        try:
            with db.execute_wrapper(wrapper):
                call_command("create_devdata", stdout=output)
            return output.getvalue()
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_command, pause_first)
        assert first_at_insert.wait(timeout=10), (
            "first command never reached its insert"
        )
        # The first command has already counted. Without a reservation the
        # second can also read zero and reach its insert on a stale snapshot.
        second = pool.submit(run_command, release_first)
        assert "Successfully created 1 services and 2 checks" in first.result()
        assert "Data already exists: 1 services, 2 checks" in second.result()

    assert Service.objects.using(alias).count() == 1
    assert HealthCheck.objects.using(alias).count() == 2
