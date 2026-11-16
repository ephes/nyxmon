from io import StringIO

import pytest
from django.core.management import call_command

from nyxboard.models import HealthCheck, Result, Service

pytestmark = pytest.mark.django_db


def legacy(service, **kwargs):
    return HealthCheck.objects.create(
        service=service, name="synthetic legacy", check_type="custom", **kwargs
    )


@pytest.mark.parametrize("action", ["delete", "disable"])
@pytest.mark.parametrize("change", [{"check_type": "http"}, {"data": {"new": 1}}, {"url": "changed"}])
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
        HealthCheck.objects.filter(pk=check.pk).update(next_check_time=123, status="processing")
        return "y"

    monkeypatch.setattr("builtins.input", confirm)
    output = StringIO()
    call_command("find_legacy_checks", delete=True, stdout=output)
    assert not HealthCheck.objects.filter(pk=check.pk).exists()
    assert "Deleted 1 legacy check(s)" in output.getvalue()
