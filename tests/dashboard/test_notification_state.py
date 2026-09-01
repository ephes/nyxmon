"""Django ownership tests for internal notification state."""

import pytest

from nyxboard.models import (
    CheckNotificationState,
    CollectorIncident,
    HealthCheck,
    Service,
)
from nyxmon.domain import CheckType


@pytest.mark.django_db
def test_notification_state_is_deleted_with_health_check() -> None:
    service = Service.objects.create(name="state cleanup")
    check = HealthCheck.objects.create(
        service=service,
        name="temporary",
        check_type=CheckType.HTTP,
        url="https://example.test/health",
    )
    CheckNotificationState.objects.create(
        health_check=check,
        failure_count=4,
        last_attempt_count=2,
        last_immediate_at=1234,
        last_notified_at=5678,
        first_failure_at=5000,
    )

    check_id = check.id
    check.delete()

    assert not CheckNotificationState.objects.filter(health_check_id=check_id).exists()


@pytest.mark.django_db
def test_reminder_timestamps_default_to_zero() -> None:
    service = Service.objects.create(name="state defaults")
    check = HealthCheck.objects.create(
        service=service,
        name="fresh",
        check_type=CheckType.HTTP,
        url="https://example.test/health",
    )

    state = CheckNotificationState.objects.create(health_check=check)

    assert state.last_notified_at == 0
    assert state.first_failure_at == 0


@pytest.mark.django_db
def test_collector_incident_is_addressable_by_key() -> None:
    CollectorIncident.objects.create(
        incident_key="stale_batch",
        opened_at=1000,
        last_alert_at=1000,
        alert_count=1,
        payload={"check_ids": [1, 2]},
    )

    stored = CollectorIncident.objects.get(pk="stale_batch")

    assert stored.payload == {"check_ids": [1, 2]}
    assert stored.alert_count == 1
