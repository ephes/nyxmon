import pytest
import sqlite3
import json
import os
from datetime import datetime, timedelta

import anyio
import aiosqlite

from nyxmon.adapters.repositories.sqlite_repo import SqliteResultRepository
from nyxmon.devdata import create_test_results


@pytest.fixture
def test_db_path(tmp_path):
    db_path = tmp_path / "test_db.sqlite"
    yield db_path
    # Clean up
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture
def sqlite_result_repo(test_db_path):
    repo = SqliteResultRepository(db_path=test_db_path)
    return repo


@pytest.fixture
def populated_db(test_db_path):
    """Create a test database with some results of different ages"""
    # Generate test data using the devdata module
    # For this test, we only need 1 recent, 1 old, and 1 very old result
    test_results = create_test_results(num_recent=1, num_old=1, num_very_old=1)

    # Create the repository and initialize the schema
    repo = SqliteResultRepository(db_path=test_db_path)

    # Initialize the database and create schema by using the repository's schema creation method
    async def init_schema():
        async with aiosqlite.connect(test_db_path) as db:
            await repo._ensure_schema(db)

    # Run the async function to initialize the schema
    anyio.run(init_schema)

    # Now connect to populate with our test data
    conn = sqlite3.connect(test_db_path)
    cursor = conn.cursor()

    # Insert the results with appropriate timestamps
    now = datetime.now()
    recent_time = now - timedelta(minutes=10)
    old_time = now - timedelta(hours=25)
    very_old_time = now - timedelta(hours=48)

    # Add the recent result (only the first one)
    if test_results["recent"]:
        result = test_results["recent"][0]
        cursor.execute(
            "INSERT INTO check_result (id, health_check_id, status, data, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                1,
                result.check_id,
                result.status,
                json.dumps(result.data),
                recent_time.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )

    # Add the old result (only the first one)
    if test_results["old"]:
        result = test_results["old"][0]
        cursor.execute(
            "INSERT INTO check_result (id, health_check_id, status, data, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                2,
                result.check_id,
                result.status,
                json.dumps(result.data),
                old_time.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )

    # Add the very old result (only the first one)
    if test_results["very_old"]:
        result = test_results["very_old"][0]
        cursor.execute(
            "INSERT INTO check_result (id, health_check_id, status, data, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                3,
                result.check_id,
                result.status,
                json.dumps(result.data),
                very_old_time.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )

    conn.commit()
    conn.close()

    return test_db_path


class TestSqliteResultRepository:
    @pytest.mark.anyio
    async def test_delete_old_results_async(self, populated_db):
        """Test that delete_old_results_async removes old results but keeps recent ones"""
        repo = SqliteResultRepository(db_path=populated_db)

        # Delete results older than 24 hours
        deleted_count = await repo.delete_old_results_async(
            retention_seconds=24 * 60 * 60
        )

        # Should have deleted 2 records (the 25-hour and 48-hour old ones)
        assert deleted_count == 2

        # Connect to DB and verify what's left
        conn = sqlite3.connect(populated_db)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM check_result")
        count = cursor.fetchone()[0]
        assert count == 1  # Only the recent result should remain

        cursor.execute("SELECT id FROM check_result")
        remaining_id = cursor.fetchone()[0]
        assert remaining_id == 1  # The recent result (id=1) should be the only one left

        conn.close()

    @pytest.mark.anyio
    async def test_delete_old_results_async_with_custom_retention(self, populated_db):
        """Test that delete_old_results_async respects custom retention period"""
        repo = SqliteResultRepository(db_path=populated_db)

        # Delete results older than 12 hours
        deleted_count = await repo.delete_old_results_async(
            retention_seconds=12 * 60 * 60
        )

        # Should have deleted 2 records (the 25-hour and 48-hour old ones)
        assert deleted_count == 2

        # Connect to DB and verify what's left
        conn = sqlite3.connect(populated_db)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM check_result")
        count = cursor.fetchone()[0]
        assert count == 1  # Only the recent result should remain

        conn.close()

    @pytest.mark.anyio
    async def test_delete_old_results_async_with_batch_size(self, populated_db):
        """Test that delete_old_results_async respects batch size"""
        repo = SqliteResultRepository(db_path=populated_db)

        # Delete results older than 12 hours, but limit to 1 record per batch
        deleted_count = await repo.delete_old_results_async(
            retention_seconds=12 * 60 * 60, batch_size=1
        )

        # Should have deleted only 1 record despite 2 being eligible
        assert deleted_count == 1

        # Connect to DB and verify what's left
        conn = sqlite3.connect(populated_db)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM check_result")
        count = cursor.fetchone()[0]
        assert count == 2  # 1 recent + 1 old result should remain

        conn.close()

    @pytest.mark.anyio
    async def test_delete_old_results_sync_wrapper(self, populated_db):
        """Test that the sync wrapper for delete_old_results works"""
        repo = SqliteResultRepository(db_path=populated_db)

        # Create a wrapper function that calls delete_old_results with our parameters
        def call_delete_old_results():
            return repo.delete_old_results(retention_seconds=24 * 60 * 60)

        # Use anyio.to_thread.run_sync to call the wrapper function
        import anyio

        deleted_count = await anyio.to_thread.run_sync(call_delete_old_results)

        # Should have deleted 2 records (the 25-hour and 48-hour old ones)
        assert deleted_count == 2

        # Connect to DB and verify what's left
        conn = sqlite3.connect(populated_db)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM check_result")
        count = cursor.fetchone()[0]
        assert count == 1  # Only the recent result should remain

        conn.close()
