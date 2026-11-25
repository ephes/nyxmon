from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, transaction

from nyxboard.models import HealthCheck, Result, Service

pytestmark = pytest.mark.django_db(transaction=True)


def legacy(service, **kwargs):
    return HealthCheck.objects.create(
        service=service, name="synthetic legacy", check_type="custom", **kwargs
    )


@pytest.mark.parametrize("action", ["delete", "disable"])
def test_cleanup_refuses_outer_transaction_before_inventory(monkeypatch, action):
    service = Service.objects.create(name="synthetic")
    check = legacy(service)
    output = StringIO()

    def unexpected_query(*args):
        pytest.fail("Cleanup started inventory inside an outer transaction")

    def unexpected_prompt(_):
        pytest.fail("Cleanup prompted inside an outer transaction")

    monkeypatch.setattr("builtins.input", unexpected_prompt)
    with transaction.atomic():
        with connection.execute_wrapper(unexpected_query):
            with pytest.raises(CommandError, match="requires autocommit"):
                call_command("find_legacy_checks", **{action: True}, stdout=output)
        assert output.getvalue() == ""
        call_command("find_legacy_checks", stdout=output)
        assert "Found 1 check(s)" in output.getvalue()

    check.refresh_from_db()
    assert not check.disabled


def test_delete_does_not_prompt_when_last_candidate_becomes_supported(monkeypatch):
    from nyxboard.management.commands import find_legacy_checks

    service = Service.objects.create(name="synthetic")
    check = legacy(service)
    temporary_file = find_legacy_checks.tempfile.TemporaryFile

    def open_receipt(*args, **kwargs):
        HealthCheck.objects.filter(pk=check.pk).update(check_type="http")
        return temporary_file(*args, **kwargs)

    def confirm(_):
        pytest.fail("Delete requested confirmation for an empty receipt")

    monkeypatch.setattr(find_legacy_checks.tempfile, "TemporaryFile", open_receipt)
    monkeypatch.setattr("builtins.input", confirm)
    output = StringIO()
    call_command("find_legacy_checks", delete=True, stdout=output)

    assert output.getvalue() == (
        "No legacy check types found. All checks are using supported types.\n"
    )
    check.refresh_from_db()
    assert check.check_type == "http"
    assert not check.disabled


def test_inventory_renders_persisted_labels_on_one_bounded_line():
    service = Service.objects.create(name="S\u2028" + "x" * 205)
    check = HealthCheck.objects.create(
        service=service, name="check\nfake row\x1b", check_type="custom\r\u202e"
    )
    output = StringIO()
    call_command("find_legacy_checks", stdout=output)

    lines = output.getvalue().splitlines()
    assert lines[0] == (
        f"  - ID: {check.pk}, Name: check?fake row?, Type: custom??, "
        f"Service: S?{'x' * 198}...[truncated 7 chars], Status: ENABLED"
    )
    assert lines[1] == "Found 1 check(s) with legacy/unsupported types."


@pytest.mark.parametrize("action", ["delete", "disable"])
@pytest.mark.parametrize(
    "change", [{"check_type": "http"}, {"data": {"new": 1}}, {"url": "changed"}]
)
def test_cleanup_preserves_changed_and_new_definitions(monkeypatch, action, change):
    service = Service.objects.create(name="synthetic")
    changed = legacy(service)
    unchanged = legacy(service)
    output = StringIO()
    edited = False
    new = None

    def edit():
        nonlocal edited, new
        if not edited:
            HealthCheck.objects.filter(pk=changed.pk).update(**change)
            new = legacy(service)
            edited = True

    def confirm(_):
        edit()
        return "y"

    monkeypatch.setattr("builtins.input", confirm)
    if action == "disable":

        class EditingOutput(StringIO):
            def write(self, text):
                result = super().write(text)
                if "ID:" in text:
                    edit()
                return result

        output = EditingOutput()
    call_command("find_legacy_checks", **{action: True}, stdout=output)
    changed.refresh_from_db()
    new.refresh_from_db()
    assert not changed.disabled
    assert not new.disabled
    if action == "delete":
        assert not HealthCheck.objects.filter(pk=unchanged.pk).exists()
    else:
        unchanged.refresh_from_db()
        assert unchanged.disabled
    assert "skipped" in output.getvalue().lower()


def test_cleanup_keeps_runtime_only_changes_and_counts_checks_not_results(monkeypatch):
    service = Service.objects.create(name="synthetic")
    check = legacy(service)
    Result.objects.create(health_check=check, status="error", data={})

    def confirm(_):
        HealthCheck.objects.filter(pk=check.pk).update(
            next_check_time=123, status="processing"
        )
        return "y"

    monkeypatch.setattr("builtins.input", confirm)
    output = StringIO()
    call_command("find_legacy_checks", delete=True, stdout=output)
    assert not HealthCheck.objects.filter(pk=check.pk).exists()
    assert "Deleted 1 legacy check(s)" in output.getvalue()


@pytest.mark.parametrize("action", ["delete", "disable"])
def test_cleanup_uses_bounded_queries_and_reports_partial_progress(monkeypatch, action):
    from nyxboard.management.commands.find_legacy_checks import Command

    service = Service.objects.create(name="synthetic fleet")
    HealthCheck.objects.bulk_create(
        [
            HealthCheck(service=service, name=f"legacy {i}", check_type="custom")
            for i in range(1205)
        ]
    )
    supported = HealthCheck.objects.create(
        service=service, name="healthy", check_type="http"
    )
    monkeypatch.setattr("builtins.input", lambda _: "y")
    original = Command._mutate_batch
    calls = 0
    max_params = 0

    def mutate(self, revisions, delete, using):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise DatabaseError("synthetic later batch failure")
        return original(self, revisions, delete, using)

    def bounded(execute, sql, params, many, context):
        nonlocal max_params
        max_params = max(max_params, len(params or ()))
        return execute(sql, params, many, context)

    monkeypatch.setattr(Command, "_mutate_batch", mutate)
    output = StringIO()
    with connection.execute_wrapper(bounded):
        with pytest.raises(CommandError, match="completed=200.*remaining=1005"):
            call_command("find_legacy_checks", **{action: True}, stdout=output)
    assert max_params <= 110
    assert (
        HealthCheck.objects.filter(check_type="custom", disabled=False).count() == 1005
    )
    supported.refresh_from_db()
    assert not supported.disabled
    assert "completed=200" in output.getvalue()


def test_dry_inventory_uses_bounded_projection_and_horizon(monkeypatch):
    service = Service.objects.create(name="synthetic")
    HealthCheck.objects.bulk_create(
        [
            HealthCheck(service=service, name=f"legacy {i}", check_type="custom")
            for i in range(205)
        ]
    )
    selects = []

    def capture(execute, sql, params, many, context):
        if sql.lstrip().startswith("SELECT") and '"health_check"' in sql:
            selects.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(capture):
        call_command("find_legacy_checks", stdout=StringIO())
    inventories = [sql for sql in selects if '"service"' in sql]
    assert len(inventories) >= 3
    assert all("LIMIT 100" in sql for sql in inventories)
    assert all('"processing_started_at"' not in sql for sql in inventories)


def test_failed_mutation_batch_rolls_back_only_that_batch(monkeypatch):

    service = Service.objects.create(name="synthetic rollback")
    HealthCheck.objects.bulk_create(
        [
            HealthCheck(service=service, name=f"legacy {i}", check_type="custom")
            for i in range(305)
        ]
    )
    writes = 0

    def fail_during_third_batch(execute, sql, params, many, context):
        nonlocal writes
        if (
            sql.startswith('UPDATE "health_check" SET "disabled" = %s')
            and params[0] is True
        ):
            writes += 1
            if writes == 250:
                raise DatabaseError("synthetic write failure")
        return execute(sql, params, many, context)

    with connection.execute_wrapper(fail_during_third_batch):
        with pytest.raises(CommandError, match="completed=200.*remaining=105"):
            call_command("find_legacy_checks", disable=True, stdout=StringIO())
    assert HealthCheck.objects.filter(disabled=True).count() == 200
    assert HealthCheck.objects.filter(disabled=False).count() == 105
