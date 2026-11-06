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

    dashboard = client.get(reverse("nyxboard:dashboard"))
    service = dashboard.context["services"][0]
    rendered_check = service.healthcheck_set.all()[0]
    assert rendered_check.get_status() == expected
    assert rendered_check.recent_results == list(prefetched_check.recent_results)
    assert rendered_check.last_result == prefetched_check.recent_results[0]


@pytest.mark.django_db
def test_dashboard_queries_stay_constant_and_reuse_rendered_checks(
    client,
    service_factory,
    healthcheck_factory,
    result_factory,
    django_assert_num_queries,
):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    service = service_factory()

    def add_check(index):
        check = healthcheck_factory(service, name=f"synthetic check {index}")
        samples = [result_factory(check, status=ResultStatus.ERROR)] + [
            result_factory(check) for _ in range(5)
        ]
        # Equal timestamps exercise the id tie-break and the per-check limit.
        Result.objects.filter(health_check=check).update(
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        return [sample.pk for sample in reversed(samples)][:5]

    expected = [add_check(0)]
    with CaptureQueriesContext(connection) as single:
        response = client.get(reverse("nyxboard:dashboard"))
    assert response.status_code == 200

    expected.extend(add_check(index) for index in range(1, 18))
    with CaptureQueriesContext(connection) as many:
        response = client.get(reverse("nyxboard:dashboard"))
    assert response.status_code == 200
    assert len(many) == len(single) == 4

    rendered_service = response.context["services"][0]
    with django_assert_num_queries(0):
        for _ in range(3):
            assert rendered_service.get_status() == StatusChoices.PASSED
        for check, ids in zip(
            rendered_service.healthcheck_set.all(), expected, strict=True
        ):
            assert [sample.pk for sample in check.recent_results] == ids
            assert check.last_result == check.recent_results[0]
            assert check.check_mode == "due"
            assert check.get_status() == StatusChoices.PASSED
            assert check.dashboard_status == StatusChoices.PASSED
