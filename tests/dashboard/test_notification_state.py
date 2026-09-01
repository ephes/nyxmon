"""Django ownership tests for internal notification state."""

import pytest

from nyxboard.models import CheckNotificationState, HealthCheck, Service
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
    )

    check_id = check.id
    check.delete()

    assert not CheckNotificationState.objects.filter(
        health_check_id=check_id
    ).exists()
