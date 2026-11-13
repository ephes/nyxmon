"""Render production list/detail templates within a constant query budget."""

from datetime import datetime, timezone

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from nyxboard.models import HealthCheck, Result, Service, StatusChoices

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("count", [1, 50, 200])
@pytest.mark.parametrize(
    "route,budget",
    [("service_list", 3), ("service_detail", 3), ("healthcheck_list", 2)],
)
def test_lists_share_bounded_recent_evidence(
    client, count, route, budget, django_assert_num_queries
):
    service = Service.objects.create(name="Synthetic fleet")
    patterns = [
        ([], StatusChoices.UNKNOWN),
        (["error"], StatusChoices.FAILED),
        (["warning"], StatusChoices.WARNING),
        (["ok"] * 4 + ["error", "ok"], StatusChoices.RECOVERING),
        (["error"] + ["ok"] * 5, StatusChoices.PASSED),
    ]
    expected = {}
    for index in range(count):
        statuses, status = patterns[index % len(patterns)]
        check = HealthCheck.objects.create(
            service=service, name=f"synthetic {index}", disabled=index % 2 == 0
        )
        samples = [
            Result.objects.create(health_check=check, status=value, data={})
            for value in statuses
        ]
        Result.objects.filter(health_check=check).update(
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        expected[check.pk] = (status, [sample.pk for sample in reversed(samples)][:5])
    url = reverse(
        f"nyxboard:{route}", args=[service.pk] if route == "service_detail" else []
    )
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url)
    assert response.status_code == 200
    assert len(queries) == budget
    assert b"Synthetic fleet" in response.content
    if route == "service_list":
        services = response.context["services"]
        checks = services[0].healthcheck_set.all()
    else:
        checks = response.context["health_checks"]
        services = [response.context["service"]] if route == "service_detail" else []
    with django_assert_num_queries(0):
        for check in checks:
            status, ids = expected[check.pk]
            assert check.get_status() == status
            assert [sample.pk for sample in check.recent_results] == ids
        for rendered in services:
            assert rendered.get_status() == (
                StatusChoices.FAILED if count > 1 else StatusChoices.UNKNOWN
            )
