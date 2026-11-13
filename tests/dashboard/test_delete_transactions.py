"""A second SQLite writer cannot change the admitted deletion mid-transaction."""

import sqlite3

import pytest
from django.db import connection, connections
from django.urls import reverse

from nyxboard import deletion
from nyxboard.models import HealthCheck, Result, Service

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def deletion_db(tmp_path, monkeypatch, django_db_blocker):
    service = Service.objects.create(name="Synthetic")
    check = HealthCheck.objects.create(service=service, name="Synthetic child")
    Result.objects.create(health_check=check, status="ok", data={})
    path = tmp_path / "delete.sqlite3"
    with sqlite3.connect(path) as copied:
        connection.connection.backup(copied)
    alias = "delete_definition"
    connections.databases[alias] = {**connection.settings_dict, "NAME": str(path)}
    for model in [Service, HealthCheck, Result]:
        monkeypatch.setattr(model, "objects", model.objects.db_manager(alias))
    with django_db_blocker.unblock():
        yield service, check, path
    connections[alias].close()
    del connections[alias]
    del connections.databases[alias]


@pytest.mark.parametrize("kind", ["service", "healthcheck"])
def test_definition_compare_and_cascade_hold_the_same_write_reservation(
    deletion_db, client, monkeypatch, kind
):
    service, check, path = deletion_db
    url = reverse(
        f"nyxboard:{kind}_delete", args=[service.pk if kind == "service" else check.pk]
    )
    token = client.get(url).context["definition_revision"]
    original_revision = deletion.deletion_revision
    attempts = []

    def race(instance, **kwargs):
        with sqlite3.connect(path, timeout=0) as writer:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                writer.execute(
                    "UPDATE health_check SET name='Concurrent' WHERE id=?", (check.pk,)
                )
        attempts.append(True)
        return original_revision(instance, **kwargs)

    monkeypatch.setattr(deletion, "deletion_revision", race)
    response = client.post(url, {"definition_revision": token})
    assert response.status_code == 302
    assert attempts == [True]
    assert not HealthCheck.objects.filter(pk=check.pk).exists()
    assert not Result.objects.exists()


@pytest.mark.parametrize("kind", ["service", "healthcheck"])
def test_second_connection_committed_change_refuses_old_confirmation(
    deletion_db, client, kind
):
    service, check, path = deletion_db
    url = reverse(
        f"nyxboard:{kind}_delete", args=[service.pk if kind == "service" else check.pk]
    )
    token = client.get(url).context["definition_revision"]
    with sqlite3.connect(path) as writer:
        writer.execute(
            "UPDATE health_check SET data=? WHERE id=?", ('{"timeout":7}', check.pk)
        )
    response = client.post(url, {"definition_revision": token})
    assert response.status_code == 409
    assert Result.objects.count() == 1
    assert HealthCheck.objects.get(pk=check.pk).data == {"timeout": 7}
