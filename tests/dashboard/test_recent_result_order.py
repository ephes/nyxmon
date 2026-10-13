"""Status agrees between model fallback and dashboard for tied samples."""

from datetime import datetime, timezone

import pytest
from django.urls import reverse

from nyxboard.models import Result, StatusChoices
from nyxmon.domain import ResultStatus


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([ResultStatus.OK, ResultStatus.ERROR], StatusChoices.FAILED),
        ([ResultStatus.ERROR, ResultStatus.OK], StatusChoices.RECOVERING),
        ([ResultStatus.ERROR] + [ResultStatus.OK] * 5, StatusChoices.PASSED),
    ],
)
def test_tied_samples_use_newest_primary_key(
    client, service_factory, healthcheck_factory, result_factory, statuses, expected
):
    check = healthcheck_factory(service_factory())
    results = [result_factory(check, status=status) for status in statuses]
    Result.objects.filter(health_check=check).update(
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )

    direct_status = check.get_status()
    response = client.get(
        reverse("nyxboard:healthcheck_update_status", kwargs={"check_id": check.pk})
    )

    assert response.status_code == 200
    prefetched_check = response.context["check"]
    assert [result.pk for result in prefetched_check.recent_results] == [
        result.pk for result in reversed(results)
    ][:5]
    assert prefetched_check.get_status() == expected
    assert direct_status == expected
