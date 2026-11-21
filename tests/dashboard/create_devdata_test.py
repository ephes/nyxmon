import sqlite3
from io import StringIO

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
