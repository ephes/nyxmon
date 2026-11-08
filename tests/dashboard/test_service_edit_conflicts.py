"""Service POSTs race a second committed connection to an isolated fixture DB."""

import sqlite3

import pytest
from django.db import connection, connections
from django.urls import reverse
from nyxboard.forms import ServiceForm
from nyxboard.models import Service

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def edit_db(tmp_path, monkeypatch, django_db_blocker):
    service = Service.objects.create(name="Before")
    path = tmp_path / "edit.sqlite3"
    with sqlite3.connect(path) as copied:
        connection.connection.backup(copied)
    alias = "service_edit"
    connections.databases[alias] = {**connection.settings_dict, "NAME": str(path)}
    monkeypatch.setattr(Service, "objects", Service.objects.db_manager(alias))
    with django_db_blocker.unblock():
        yield service, path
    connections[alias].close()
    del connections[alias]
    del connections.databases[alias]


def form_data(client, service):
    url = reverse("nyxboard:service_update", args=[service.pk])
    form = client.get(url).context["form"]
    # The optional lookup also lets these regressions execute the old view.
    revision = (
        form["definition_revision"].value()
        if "definition_revision" in form.fields
        else ""
    )
    return url, {"name": "Submitted", "definition_revision": revision}


def assert_conflict(response):
    assert response.status_code == 409
    assert response.context["form"]["name"].value() == "Submitted"
    assert "reload and reconcile" in response.content.decode()


@pytest.mark.parametrize("delete", [True, False])
def test_committed_change_after_validation_cannot_be_overwritten(
    edit_db, client, monkeypatch, delete
):
    service, path = edit_db
    url, data = form_data(client, service)
    save = ServiceForm.save
    raced = []

    def concurrent_save(self, *args, **kwargs):
        with sqlite3.connect(path) as writer:
            sql = (
                "DELETE FROM service WHERE id=?"
                if delete
                else "UPDATE service SET name='Other' WHERE id=?"
            )
            writer.execute(sql, (service.pk,))
        raced.append(True)
        return save(self, *args, **kwargs)

    monkeypatch.setattr(ServiceForm, "save", concurrent_save)
    response = client.post(url, data)
    assert raced == [True], "the competing committed write must execute"
    assert_conflict(response)
    if delete:
        assert not Service.objects.filter(pk=service.pk).exists()
    else:
        assert Service.objects.get(pk=service.pk).name == "Other"


def test_rename_after_get_requires_reconciliation(edit_db, client):
    service, path = edit_db
    url, data = form_data(client, service)
    with sqlite3.connect(path) as writer:
        writer.execute("UPDATE service SET name='Other' WHERE id=?", (service.pk,))
    assert_conflict(client.post(url, data))
    assert Service.objects.get(pk=service.pk).name == "Other"


def test_delete_before_post_retains_submitted_name(edit_db, client):
    service, path = edit_db
    url, data = form_data(client, service)
    with sqlite3.connect(path) as writer:
        writer.execute("DELETE FROM service WHERE id=?", (service.pk,))
    assert_conflict(client.post(url, data))
    assert not Service.objects.filter(pk=service.pk).exists()


def test_service_update_requires_browser_revision(edit_db, client):
    service, _ = edit_db
    url = reverse("nyxboard:service_update", args=[service.pk])
    response = client.post(url, {"name": "Submitted"})
    assert response.status_code == 200
    assert "definition_revision" in response.context["form"].errors
    assert Service.objects.get(pk=service.pk).name == "Before"
