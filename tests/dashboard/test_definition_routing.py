"""Definition mutations use one authoritative transaction despite read routing."""

import sqlite3
from types import SimpleNamespace

import pytest
from django.db import connection, connections
from django.test.utils import CaptureQueriesContext

from nyxboard import deletion
from nyxboard.models import HealthCheck, Result, Service

pytestmark = pytest.mark.django_db(transaction=True)


class DefinitionRouter:
    def db_for_read(self, model, **hints):
        return "definition_replica"

    def db_for_write(self, model, **hints):
        return "definition_writer"


@pytest.fixture
def routed_definitions(tmp_path, settings, django_db_blocker):
    service = Service.objects.create(name="Synthetic service")
    check = HealthCheck.objects.create(
        service=service,
        name="Synthetic check",
        url="https://example.invalid",
        data={"label": "café", "timeout": 7},
        next_check_time=9000,
    )
    Result.objects.create(health_check=check, status="ok", data={})
    paths = {}
    for alias in ("definition_writer", "definition_replica"):
        path = tmp_path / f"{alias}.sqlite3"
        with sqlite3.connect(path) as copied:
            connection.connection.backup(copied)
        connections.databases[alias] = {**connection.settings_dict, "NAME": str(path)}
        paths[alias] = path
    settings.DATABASE_ROUTERS = [DefinitionRouter()]
    try:
        with django_db_blocker.unblock():
            yield SimpleNamespace(
                service=service,
                check=check,
                writer="definition_writer",
                replica="definition_replica",
                path=paths["definition_writer"],
            )
    finally:
        for alias in paths:
            connections[alias].close()
            del connections[alias]
            del connections.databases[alias]


def confirmed_definition(db, kind):
    model, pk = (
        (Service, db.service.pk) if kind == "service" else (HealthCheck, db.check.pk)
    )
    instance = model.objects.using(db.replica).get(pk=pk)
    return model, pk, deletion.deletion_revision(instance)


@pytest.mark.parametrize("kind", ["service", "healthcheck"])
def test_routed_delete_holds_writer_until_cascade(
    routed_definitions, monkeypatch, kind
):
    db = routed_definitions
    model, pk, token = confirmed_definition(db, kind)
    original_revision = deletion.deletion_revision
    attempts = []

    def race(instance, **kwargs):
        with sqlite3.connect(db.path, timeout=0) as writer:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                writer.execute(
                    "UPDATE health_check SET name='Concurrent' WHERE id=?",
                    (db.check.pk,),
                )
        attempts.append(True)
        return original_revision(instance, **kwargs)

    monkeypatch.setattr(deletion, "deletion_revision", race)
    with (
        CaptureQueriesContext(connection) as default_queries,
        CaptureQueriesContext(connections[db.replica]) as replica_queries,
    ):
        instance, deleted = deletion.delete_confirmed(model, pk, token)
    assert deleted
    assert instance._state.db == db.writer
    assert attempts == [True]
    assert not default_queries and not replica_queries
    assert not HealthCheck.objects.using(db.writer).exists()
    assert not Result.objects.using(db.writer).exists()
    for alias in ("default", db.replica):
        assert Service.objects.using(alias).count() == 1
        assert HealthCheck.objects.using(alias).count() == 1
        assert Result.objects.using(alias).count() == 1


@pytest.mark.parametrize("kind", ["service", "healthcheck"])
def test_routed_delete_rechecks_writer_not_stale_replica(routed_definitions, kind):
    db = routed_definitions
    model, pk, token = confirmed_definition(db, kind)
    HealthCheck.objects.using(db.writer).filter(pk=db.check.pk).update(
        data={"timeout": 9}
    )
    _, deleted = deletion.delete_confirmed(model, pk, token)
    assert not deleted
    assert HealthCheck.objects.using(db.writer).get(pk=db.check.pk).data == {
        "timeout": 9
    }
    assert Service.objects.using(db.writer).count() == 1
    assert Result.objects.using(db.writer).count() == 1


@pytest.mark.parametrize("kind", ["service", "healthcheck"])
def test_routed_delete_failure_rolls_back_cascade(
    routed_definitions, monkeypatch, kind
):
    db = routed_definitions
    model, pk, token = confirmed_definition(db, kind)
    original_delete = model.delete

    def fail_delete(instance, *args, **kwargs):
        original_delete(instance, *args, **kwargs)
        raise RuntimeError("synthetic cascade failure")

    monkeypatch.setattr(model, "delete", fail_delete)
    with pytest.raises(RuntimeError, match="synthetic cascade failure"):
        deletion.delete_confirmed(model, pk, token)
    for alias in ("default", db.writer, db.replica):
        assert Service.objects.using(alias).count() == 1
        assert HealthCheck.objects.using(alias).count() == 1
        assert Result.objects.using(alias).count() == 1
