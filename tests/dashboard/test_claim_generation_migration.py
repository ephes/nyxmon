"""Synthetic existing claims acquire identity without changing lease timing."""

import uuid

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_existing_claims_get_distinct_generations():
    old = ("nyxboard", "0014_login_throttle_bucket")
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([old])
        apps = executor.loader.project_state([old]).apps
        service = apps.get_model("nyxboard", "Service").objects.create(name="synthetic")
        checks = apps.get_model("nyxboard", "HealthCheck")
        for i, status in enumerate(["processing", "processing", "idle"], 1):
            checks.objects.create(
                id=i,
                service=service,
                name=f"synthetic {i}",
                status=status,
                processing_started_at=1000,
            )
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        checks = executor.loader.project_state(latest).apps.get_model(
            "nyxboard", "HealthCheck"
        )
        rows = list(
            checks.objects.order_by("id").values_list(
                "claim_generation", "processing_started_at"
            )
        )
        assert rows[0][0] != rows[1][0]
        assert all(uuid.UUID(token).hex == token for token, _ in rows[:2])
        assert rows[2][0] == ""
        assert [timestamp for _, timestamp in rows] == [1000, 1000, 1000]
    finally:
        MigrationExecutor(connection).migrate(latest)
