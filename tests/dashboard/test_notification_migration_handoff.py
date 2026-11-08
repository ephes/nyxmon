"""Historical Django upgrade fixture and optional live Rust decision handoff."""

import os
import subprocess
from pathlib import Path

import pytest
from django.db import connection, connections
from django.db.migrations.executor import MigrationExecutor

REPO = Path(__file__).resolve().parents[2]
UPGRADE_TIME = 1_800_000_000


@pytest.mark.django_db(transaction=True)
def test_historical_reminder_state_matches_rust_fixture(tmp_path):
    alias = "rust_migration_handoff"
    path = tmp_path / "historical.sqlite3"
    connections.databases[alias] = {**connection.settings_dict, "NAME": str(path)}
    migrated = connections[alias]
    try:
        migrated.connect()
        executor = MigrationExecutor(migrated)
        old = ("nyxboard", "0011_checknotificationstate")
        executor.migrate([old])
        apps = executor.loader.project_state([old]).apps
        service = (
            apps.get_model("nyxboard", "Service")
            .objects.using(alias)
            .create(name="synthetic")
        )
        checks = apps.get_model("nyxboard", "HealthCheck")
        states = apps.get_model("nyxboard", "CheckNotificationState")
        for check_id, failures, attempts in [(1, 121, 121), (2, 1, 0)]:
            checks.objects.using(alias).create(
                id=check_id,
                service=service,
                name=f"synthetic {check_id}",
                url="https://example.invalid",
                data={},
            )
            states.objects.using(alias).create(
                health_check_id=check_id,
                failure_count=failures,
                last_attempt_count=attempts,
                last_immediate_at=0,
            )
        # Pin only this scratch connection's SQLite wall clock, not the host.
        migrated.connection.create_function(
            "strftime", 2, lambda fmt, value: str(UPGRADE_TIME)
        )
        executor = MigrationExecutor(migrated)
        executor.migrate(executor.loader.graph.leaf_nodes())
        dump = list(migrated.connection.iterdump())
        projection = (
            "\n".join(
                line
                for table in ("service", "health_check", "check_notification_state")
                for line in dump
                if line.startswith(f'INSERT INTO "{table}"')
            )
            + "\n"
        )
        (tmp_path / "projection.sql").write_text(projection)
        fixture = REPO / "agent-rs" / "fixtures" / "notification_reminder_upgrade.sql"
        assert projection == fixture.read_text()
        migrated.close()
        if os.environ.get("NYXMON_TEST_LIVE_HANDOFF") != "1":
            return
        subprocess.run(
            [
                "cargo",
                "test",
                "--locked",
                "-p",
                "nyxmon-agent",
                "--test",
                "notification_journeys",
                "migrated_reminder_state",
                "--",
                "--exact",
                "--nocapture",
            ],
            cwd=REPO / "agent-rs",
            check=True,
            timeout=600,
            env={
                **os.environ,
                "NYXMON_MIGRATED_DB": str(path),
                "NYXMON_MIGRATION_NOW": str(UPGRADE_TIME),
            },
        )
    finally:
        migrated.close()
        del connections[alias]
        del connections.databases[alias]
