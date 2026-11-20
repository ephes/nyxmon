"""Definition mutations use one authoritative transaction despite read routing."""

import sqlite3
from types import SimpleNamespace

import pytest
from django.db import connection, connections
from django.test.utils import CaptureQueriesContext

from nyxboard import deletion
from nyxboard.forms import DefinitionConflict, HttpHealthCheckForm, ServiceForm
from nyxboard.models import HealthCheck, Result, Service

pytestmark = pytest.mark.django_db(transaction=True)


class DefinitionRouter:
    def db_for_read(self, model, **hints):
        return "definition_replica"

    def db_for_write(self, model, **hints):
        return "definition_writer"

    def allow_relation(self, obj1, obj2, **hints):
        return True  # All aliases represent the same replicated database.


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


@pytest.mark.parametrize("stale", [False, True])
def test_service_edit_uses_instance_write_route(routed_definitions, settings, stale):
    db = routed_definitions

    class ServiceEditRouter(DefinitionRouter):
        def db_for_write(self, model, **hints):
            return db.writer if hints.get("instance") is not None else db.replica

    settings.DATABASE_ROUTERS = [ServiceEditRouter()]
    service = Service.objects.using(db.replica).get(pk=db.service.pk)
    form = ServiceForm({"name": "Edited"}, instance=service)
    assert form.is_valid(), form.errors
    writer_row = Service.objects.using(db.writer).filter(pk=service.pk)
    if stale:
        writer_row.update(name="Concurrent")
        with pytest.raises(DefinitionConflict):
            form.save()
    else:
        form.save()
    assert writer_row.get().name == ("Concurrent" if stale else "Edited")
    for alias in ("default", db.replica):
        assert Service.objects.using(alias).get(pk=service.pk).name == db.service.name


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


def edit_form(db, *, source=None):
    check = HealthCheck.objects.using(source or db.writer).get(pk=db.check.pk)
    form = HttpHealthCheckForm(
        {
            "name": "Edited",
            "service": check.service_id,
            "check_type": "http",
            "url": check.url,
            "check_interval": 60,
            "definition_revision": HttpHealthCheckForm(instance=check)._revision,
        },
        instance=check,
        require_revision=True,
    )
    assert form.is_valid(), form.errors
    return form


@pytest.mark.parametrize(
    "runtime",
    [
        dict(status="processing", processing_started_at=111, next_check_time=9000),
        dict(status="idle", processing_started_at=0, next_check_time=12000),
    ],
)
@pytest.mark.parametrize("different_replica_json", [False, True])
def test_routed_save_admits_writer_json_and_returns_current_runtime(
    routed_definitions, runtime, different_replica_json
):
    db = routed_definitions
    form = edit_form(db)
    if different_replica_json:
        HealthCheck.objects.using(db.replica).filter(pk=db.check.pk).update(
            data={"replica": "outdated"}
        )
    runtime = {**runtime, "claim_generation": "a" * 32}
    HealthCheck.objects.using(db.writer).filter(pk=db.check.pk).update(**runtime)
    # Rust's compact JSON has different text but the same decoded definition.
    with connections[db.writer].cursor() as cursor:
        cursor.execute(
            "UPDATE health_check SET data=%s WHERE id=%s",
            ['{"timeout":7,"label":"café"}', db.check.pk],
        )
    admissions = []

    def check_transaction(execute, sql, params, many, context):
        if sql.lstrip().upper().startswith("SELECT"):
            admissions.append(context["connection"].in_atomic_block)
        return execute(sql, params, many, context)

    with (
        connections[db.writer].execute_wrapper(check_transaction),
        CaptureQueriesContext(connection) as default_queries,
        CaptureQueriesContext(connections[db.replica]) as replica_queries,
    ):
        saved = form.save()
    assert not default_queries and not replica_queries
    assert admissions and all(admissions)
    assert saved._state.db == db.writer
    assert saved.name == "Edited"
    assert saved.check_interval == 60
    assert saved.data == {}  # The HTTP form clears its legacy configuration.
    assert {key: getattr(saved, key) for key in runtime} == runtime
    stored = HealthCheck.objects.using(db.writer).get(pk=db.check.pk)
    assert stored.name == saved.name
    assert stored.data == saved.data
    assert {key: getattr(stored, key) for key in runtime} == runtime
    for alias in ("default", db.replica):
        assert (
            HealthCheck.objects.using(alias).get(pk=db.check.pk).name == db.check.name
        )


def test_routed_save_refuses_stale_replica_definition(routed_definitions):
    db = routed_definitions
    form = edit_form(db, source=db.replica)
    HealthCheck.objects.using(db.writer).filter(pk=db.check.pk).update(
        data={"timeout": 9}
    )
    with pytest.raises(DefinitionConflict):
        form.save()
    stored = HealthCheck.objects.using(db.writer).get(pk=db.check.pk)
    assert stored.name == db.check.name
    assert stored.data == {"timeout": 9}


def test_routed_save_readback_failure_rolls_back_writer(
    routed_definitions, monkeypatch
):
    db = routed_definitions
    form = edit_form(db)
    readbacks = []

    def fail_readback(instance, *args, **kwargs):
        # The update really happened before the injected readback failure.
        stored = HealthCheck.objects.using(db.writer).get(pk=instance.pk)
        readbacks.append((stored.name, stored.check_interval))
        raise RuntimeError("synthetic readback failure")

    monkeypatch.setattr(HealthCheck, "refresh_from_db", fail_readback)
    with pytest.raises(RuntimeError, match="synthetic readback failure"):
        form.save()
    assert readbacks == [("Edited", 60)]
    for alias in ("default", db.writer, db.replica):
        stored = HealthCheck.objects.using(alias).get(pk=db.check.pk)
        assert stored.name == db.check.name
        assert stored.check_interval == db.check.check_interval
        assert stored.next_check_time == db.check.next_check_time
        assert stored.data == db.check.data
