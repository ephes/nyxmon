"""Tests for SQLite repository data field handling."""

import pytest
import json
import tempfile
from pathlib import Path

from nyxmon.adapters.repositories.sqlite_repo import SqliteCheckRepository
from nyxmon.domain import Check, CheckType, CheckStatus


@pytest.fixture
def temp_db():
    """Create a temporary database file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    # Cleanup
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def check_repo(temp_db):
    """Create a check repository with a temporary database."""
    return SqliteCheckRepository(temp_db)


class TestCheckDataRoundTrip:
    """Integration tests for Check.data round-trip through repository."""

    @pytest.mark.anyio
    async def test_data_round_trip_via_repository(self, check_repo):
        """Test that Check.data is properly serialized and deserialized via repository.

        This ensures JSON serialization/deserialization works end-to-end.
        Protects against future regressions in row_to_check() or _add_async().
        """
        # Create a Check with non-empty data (DNS config)
        dns_config = {
            "expected_ips": ["192.168.1.100", "192.168.1.101"],
            "dns_server": "8.8.8.8",
            "source_ip": "192.168.1.50",
            "query_type": "A",
            "timeout": 10.0,
        }

        original_check = Check(
            check_id=1,
            service_id=1,
            name="Test DNS Check",
            check_type=CheckType.DNS,
            url="example.com",
            check_interval=300,
            status=CheckStatus.IDLE,
            next_check_time=0,
            processing_started_at=0,
            disabled=False,
            data=dns_config,
        )

        # Add to repository
        await check_repo._add_async(original_check)

        # Retrieve from repository
        checks = await check_repo.list_async()

        # Assert data matches original
        assert len(checks) == 1
        retrieved_check = checks[0]
        assert retrieved_check.data == dns_config
        assert retrieved_check.data["expected_ips"] == [
            "192.168.1.100",
            "192.168.1.101",
        ]
        assert retrieved_check.data["dns_server"] == "8.8.8.8"
        assert retrieved_check.data["source_ip"] == "192.168.1.50"
        assert retrieved_check.data["query_type"] == "A"
        assert retrieved_check.data["timeout"] == 10.0

    @pytest.mark.anyio
    async def test_data_round_trip_via_list_due_checks(self, check_repo):
        """Test that Check.data is populated in list_due_checks_async().

        Critical for agent operation - ensures DNS config is available
        when checks are claimed for execution.
        """
        dns_config = {
            "expected_ips": ["192.168.1.100"],
            "query_type": "A",
            "timeout": 5.0,
        }

        check = Check(
            check_id=1,
            service_id=1,
            name="Due DNS Check",
            check_type=CheckType.DNS,
            url="example.com",
            check_interval=300,
            status=CheckStatus.IDLE,
            next_check_time=0,  # Due now
            processing_started_at=0,
            disabled=False,
            data=dns_config,
        )

        await check_repo._add_async(check)

        # Get due checks
        due_checks = await check_repo.list_due_checks_async()

        # Assert data is populated
        assert len(due_checks) == 1
        retrieved_check = due_checks[0]
        assert retrieved_check.data == dns_config

    @pytest.mark.anyio
    async def test_data_handles_empty_dict(self, check_repo):
        """Test that empty data dict is handled correctly."""
        check = Check(
            check_id=1,
            service_id=1,
            name="HTTP Check",
            check_type=CheckType.HTTP,
            url="https://example.com",
            check_interval=300,
            status=CheckStatus.IDLE,
            next_check_time=0,
            processing_started_at=0,
            disabled=False,
            data={},  # Empty dict for HTTP checks
        )

        await check_repo._add_async(check)

        checks = await check_repo.list_async()
        assert len(checks) == 1
        assert checks[0].data == {}

    @pytest.mark.anyio
    @pytest.mark.django_db(transaction=True)
    async def test_django_created_row_data_flow(self):
        """Test Django ORM → Repository data flow.

        Ensures that checks created via Django ORM can be read by the repository
        with data field properly populated.

        This locks in the data-flow guarantee: Django writes → SQLite file → Repository reads

        """
        from asgiref.sync import sync_to_async
        from django.db import connection

        from nyxboard.models import HealthCheck, Service

        service = await sync_to_async(Service.objects.create)(
            name="Repo Integration Service"
        )

        dns_config = {
            "expected_ips": ["192.168.1.100", "192.168.1.101"],
            "dns_server": "8.8.8.8",
            "source_ip": "192.168.1.50",
            "query_type": "A",
            "timeout": 5.0,
        }

        django_check = await sync_to_async(HealthCheck.objects.create)(
            name="DNS via Django ORM",
            service=service,
            check_type=CheckType.DNS,
            url="example.com",
            check_interval=300,
            status=CheckStatus.IDLE,
            next_check_time=0,
            processing_started_at=0,
            disabled=False,
            data=dns_config,
        )

        db_name = connection.settings_dict["NAME"]
        repo = SqliteCheckRepository(Path(db_name))

        checks = await repo.list_async()

        matching = [c for c in checks if c.check_id == django_check.id]
        assert matching, "Repository did not return Django-created health check"
        assert matching[0].data == dns_config


class TestCheckDataMigration:
    """Tests for handling missing data column during migration."""

    @pytest.mark.anyio
    async def test_row_to_check_handles_missing_data_column(self, temp_db):
        """Test that row_to_check() handles missing data column gracefully.

        This ensures backward compatibility during migration from databases
        without the data column.
        """
        import aiosqlite

        # Create a database with old schema (no data column)
        async with aiosqlite.connect(temp_db) as db:
            await db.execute(
                """
                CREATE TABLE health_check (
                    id INTEGER PRIMARY KEY,
                    service_id INTEGER NOT NULL,
                    name TEXT DEFAULT '',
                    check_type TEXT NOT NULL,
                    url TEXT NOT NULL,
                    check_interval INTEGER NOT NULL,
                    status TEXT DEFAULT 'idle',
                    next_check_time INTEGER DEFAULT 0,
                    processing_started_at INTEGER DEFAULT 0,
                    disabled INTEGER DEFAULT 0
                )
                """
            )

            # Insert a check without data column
            await db.execute(
                """
                INSERT INTO health_check
                (id, service_id, name, check_type, url, check_interval, status, next_check_time, processing_started_at, disabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    1,
                    "Old Check",
                    "http",
                    "https://example.com",
                    300,
                    "idle",
                    0,
                    0,
                    0,
                ),
            )
            await db.commit()

        # Try to read with repository
        repo = SqliteCheckRepository(temp_db)
        checks = await repo.list_async()

        # Should succeed with empty data dict
        assert len(checks) == 1
        assert checks[0].data == {}
        assert checks[0].name == "Old Check"

    @pytest.mark.anyio
    async def test_ensure_schema_adds_data_column(self, temp_db):
        """Test that _ensure_schema() adds data column to existing tables."""
        import aiosqlite

        # Create database with old schema
        async with aiosqlite.connect(temp_db) as db:
            await db.execute(
                """
                CREATE TABLE health_check (
                    id INTEGER PRIMARY KEY,
                    service_id INTEGER NOT NULL,
                    name TEXT DEFAULT '',
                    check_type TEXT NOT NULL,
                    url TEXT NOT NULL,
                    check_interval INTEGER NOT NULL,
                    status TEXT DEFAULT 'idle',
                    next_check_time INTEGER DEFAULT 0,
                    processing_started_at INTEGER DEFAULT 0,
                    disabled INTEGER DEFAULT 0
                )
                """
            )
            await db.commit()

        # Initialize repository (should run migration)
        repo = SqliteCheckRepository(temp_db)

        # Verify data column was added
        async with aiosqlite.connect(temp_db) as db:
            await repo._ensure_schema(db)

            cursor = await db.execute("PRAGMA table_info(health_check)")
            columns = await cursor.fetchall()
            column_names = [col[1] for col in columns]

            assert "data" in column_names

    @pytest.mark.anyio
    async def test_row_to_check_parses_json_data(self, check_repo):
        """Test that row_to_check() correctly parses JSON data."""
        import aiosqlite

        # Insert a check with JSON data directly
        async with aiosqlite.connect(check_repo._db_path) as db:
            await check_repo._ensure_schema(db)

            dns_config = {
                "expected_ips": ["192.168.1.100"],
                "query_type": "A",
                "timeout": 5.0,
            }

            await db.execute(
                """
                INSERT INTO health_check
                (id, service_id, name, check_type, url, check_interval, status, next_check_time, processing_started_at, disabled, data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    1,
                    "Test Check",
                    "dns",
                    "example.com",
                    300,
                    "idle",
                    0,
                    0,
                    0,
                    json.dumps(dns_config),  # JSON string
                ),
            )
            await db.commit()

        # Read via repository
        checks = await check_repo.list_async()

        # Should parse JSON correctly
        assert len(checks) == 1
        assert checks[0].data == dns_config

    @pytest.mark.anyio
    async def test_row_to_check_handles_null_data(self, check_repo):
        """Test that row_to_check() handles NULL data (returns empty dict)."""
        import aiosqlite

        # Insert a check with NULL data
        async with aiosqlite.connect(check_repo._db_path) as db:
            await check_repo._ensure_schema(db)

            await db.execute(
                """
                INSERT INTO health_check
                (id, service_id, name, check_type, url, check_interval, status, next_check_time, processing_started_at, disabled, data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    1,
                    "Test Check",
                    "http",
                    "https://example.com",
                    300,
                    "idle",
                    0,
                    0,
                    0,
                    None,
                ),
            )
            await db.commit()

        # Read via repository
        checks = await check_repo.list_async()

        # Should return empty dict for NULL data
        assert len(checks) == 1
        assert checks[0].data == {}
