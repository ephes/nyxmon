"""Tests for SQLite repository data field handling."""

import pytest
import aiosqlite
import json
import sqlite3
import tempfile
import time
from pathlib import Path

from anyio.from_thread import BlockingPortalProvider
from nyxmon.adapters.repositories.sqlite_repo import SqliteCheckRepository, SqliteStore
from nyxmon.adapters.repositories.interface import (
    NotificationState,
    NotificationStateConflict,
)
from nyxmon.domain import Check, CheckStatus, CheckType, Result, ResultStatus


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
    async def test_reclaims_only_expired_processing_leases(self, check_repo):
        now = int(time.time())
        stale = Check(
            check_id=1,
            service_id=1,
            name="Stale",
            check_type=CheckType.HTTP,
            url="https://example.com/stale",
            status=CheckStatus.PROCESSING,
            processing_started_at=now - 601,
            data={},
        )
        active = Check(
            check_id=2,
            service_id=1,
            name="Active",
            check_type=CheckType.HTTP,
            url="https://example.com/active",
            status=CheckStatus.PROCESSING,
            processing_started_at=now - 10,
            data={},
        )
        missing_timestamp = Check(
            check_id=3,
            service_id=1,
            name="Missing timestamp",
            check_type=CheckType.HTTP,
            url="https://example.com/missing-timestamp",
            status=CheckStatus.PROCESSING,
            processing_started_at=0,
            data={},
        )
        await check_repo._add_async(stale)
        await check_repo._add_async(active)
        await check_repo._add_async(missing_timestamp)

        reclaimed = await check_repo.reclaim_stale_checks_async(300)

        assert {check.check_id for check in reclaimed} == {stale.check_id}
        checks = {check.check_id: check for check in await check_repo.list_async()}
        assert checks[stale.check_id].status == CheckStatus.IDLE
        assert checks[stale.check_id].processing_started_at == 0
        assert checks[active.check_id].status == CheckStatus.PROCESSING
        assert checks[missing_timestamp.check_id].status == CheckStatus.PROCESSING
        assert checks[missing_timestamp.check_id].processing_started_at >= now

    @pytest.mark.anyio
    async def test_claim_completion_and_expiry_lifecycle(self, check_repo, monkeypatch):
        check = Check(
            check_id=1,
            service_id=1,
            name="Lease lifecycle",
            check_type=CheckType.HTTP,
            url="https://example.com/lifecycle",
            next_check_time=0,
            data={},
        )
        await check_repo._add_async(check)
        now = 10_000
        monkeypatch.setattr(
            "nyxmon.adapters.repositories.sqlite_repo.current_epoch", lambda: now
        )

        [claimed] = await check_repo.list_due_checks_async()
        assert claimed.status == CheckStatus.PROCESSING
        assert claimed.processing_started_at == now
        assert await check_repo.reclaim_stale_checks_async(300) == []

        now += 301
        [reclaimed] = await check_repo.reclaim_stale_checks_async(300)
        assert reclaimed.check_id == check.check_id
        assert reclaimed.status == CheckStatus.IDLE
        assert reclaimed.processing_started_at == 0

        reclaimed.next_check_time = 0
        await check_repo._add_async(reclaimed)
        [claimed_again] = await check_repo.list_due_checks_async()
        claimed_again.schedule_next_check()
        await check_repo._add_async(claimed_again)
        [completed] = await check_repo.list_async()
        assert completed.status == CheckStatus.IDLE
        assert completed.processing_started_at == 0

    @pytest.mark.anyio
    async def test_notification_state_is_separate_from_check_data(self, check_repo):
        await check_repo._add_async(
            Check(
                check_id=7,
                service_id=1,
                name="State owner",
                check_type=CheckType.HTTP,
                url="https://example.test/state",
                data={},
            )
        )
        await check_repo._set_notification_state_async(7, NotificationState(4, 2, 1234))
        assert await check_repo._get_notification_state_async(7) == NotificationState(
            4, 2, 1234
        )

        updated = (await check_repo.list_async())[0]
        updated.name = "State owner updated"
        await check_repo._add_async(updated)
        assert await check_repo._get_notification_state_async(7) == NotificationState(
            4, 2, 1234
        )

    @pytest.mark.anyio
    async def test_check_upsert_preserves_fk_children(self, temp_db):
        store = SqliteStore(temp_db)
        check = Check(
            check_id=1,
            service_id=1,
            name="FK owner",
            check_type=CheckType.HTTP,
            url="https://example.test/fk-owner",
            data={},
        )
        await store.checks._add_async(check)
        await store._persist_check_result_async(
            check,
            Result(check_id=1, status=ResultStatus.ERROR, data={}),
            (NotificationState(), NotificationState(1, 1, 123)),
        )
        check.name = "FK owner updated"

        async with aiosqlite.connect(temp_db) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await store.checks._upsert_on_connection(db, check)
            await db.commit()
            state_count = await db.execute_fetchall(
                "SELECT COUNT(*) FROM check_notification_state WHERE check_id = 1"
            )
            result_count = await db.execute_fetchall(
                "SELECT COUNT(*) FROM check_result WHERE health_check_id = 1"
            )

        assert state_count[0][0] == 1
        assert result_count[0][0] == 1

    @pytest.mark.anyio
    async def test_result_and_notification_state_are_atomic(self, temp_db):
        store = SqliteStore(temp_db)
        check = Check(
            check_id=1,
            service_id=1,
            name="Atomic",
            check_type=CheckType.HTTP,
            url="https://example.test/atomic",
            data={},
        )
        result = Result(
            check_id=1,
            status=ResultStatus.ERROR,
            data={"error_type": "test"},
        )
        await store.checks._add_async(check)

        await store._persist_check_result_async(
            check, result, (NotificationState(), NotificationState(2, 2, 0))
        )

        assert len(await store.results._list_async()) == 1
        assert await store.checks._get_notification_state_async(1) == NotificationState(
            2, 2, 0
        )

    @pytest.mark.anyio
    async def test_state_write_failure_rolls_back_result_and_check(self, temp_db):
        store = SqliteStore(temp_db)
        async with aiosqlite.connect(temp_db) as db:
            await store.checks._ensure_schema(db)
            await store.results._ensure_schema(db)
            await db.execute(
                """CREATE TRIGGER reject_notification_state
                   BEFORE INSERT ON check_notification_state
                   BEGIN
                       SELECT RAISE(ABORT, 'state write rejected');
                   END"""
            )
            await db.commit()

        check = Check(
            check_id=1,
            service_id=1,
            name="Atomic rollback",
            check_type=CheckType.HTTP,
            url="https://example.test/rollback",
            data={},
        )
        result = Result(
            check_id=1,
            status=ResultStatus.ERROR,
            data={"error_type": "test"},
        )
        await store.checks._add_async(check)

        with pytest.raises(sqlite3.IntegrityError, match="state write rejected"):
            await store._persist_check_result_async(
                check, result, (NotificationState(), NotificationState(1, 1, 0))
            )

        assert len(await store.checks.list_async()) == 1
        assert await store.results._list_async() == []

    @pytest.mark.anyio
    async def test_stale_notification_state_is_rejected_atomically(self, temp_db):
        store = SqliteStore(temp_db)
        check = Check(
            check_id=1,
            service_id=1,
            name="CAS owner",
            check_type=CheckType.HTTP,
            url="https://example.test/cas",
            data={},
        )
        await store.checks._add_async(check)
        await store._persist_check_result_async(
            check,
            Result(check_id=1, status=ResultStatus.ERROR, data={}),
            (NotificationState(), NotificationState(1, 0, 0)),
        )
        check.name = "must roll back"

        with pytest.raises(NotificationStateConflict):
            await store._persist_check_result_async(
                check,
                Result(check_id=1, status=ResultStatus.ERROR, data={}),
                (NotificationState(), NotificationState(1, 1, 0)),
            )

        [persisted_check] = await store.checks.list_async()
        assert persisted_check.name == "CAS owner"
        assert len(await store.results._list_async()) == 1
        assert await store.checks._get_notification_state_async(1) == NotificationState(
            1, 0, 0
        )

    @pytest.mark.anyio
    async def test_conflict_is_detected_on_reminder_timestamps_alone(self, temp_db):
        """The compare-and-swap covers every field of the state record.

        A concurrent writer that only moved ``last_notified_at`` must still be
        detected, or two workers could each claim the same reminder window.
        """
        store = SqliteStore(temp_db)
        check = Check(
            check_id=1,
            service_id=1,
            name="Reminder CAS",
            check_type=CheckType.HTTP,
            url="https://example.test/reminder-cas",
            data={},
        )
        await store.checks._add_async(check)
        await store.checks._set_notification_state_async(
            1, NotificationState(3, 3, 0, 5_000, 4_000)
        )

        stale_expectation = NotificationState(3, 3, 0, 1_000, 4_000)
        with pytest.raises(NotificationStateConflict):
            await store._persist_check_result_async(
                check,
                Result(check_id=1, status=ResultStatus.ERROR, data={}),
                (stale_expectation, NotificationState(4, 4, 0, 9_000, 4_000)),
                complete_check=False,
            )

        assert await store.checks._get_notification_state_async(1) == NotificationState(
            3, 3, 0, 5_000, 4_000
        )
        assert await store.results._list_async() == []

    @pytest.mark.anyio
    async def test_reminder_timestamps_round_trip_through_persist(self, temp_db):
        store = SqliteStore(temp_db)
        check = Check(
            check_id=1,
            service_id=1,
            name="Reminder round trip",
            check_type=CheckType.HTTP,
            url="https://example.test/reminder-round-trip",
            data={},
        )
        await store.checks._add_async(check)

        assert await store._persist_check_result_async(
            check,
            Result(check_id=1, status=ResultStatus.ERROR, data={}),
            (NotificationState(), NotificationState(1, 1, 0, 7_000, 7_000)),
        )

        assert await store.checks._get_notification_state_async(1) == NotificationState(
            1, 1, 0, 7_000, 7_000
        )

    @pytest.mark.anyio
    async def test_notification_state_is_deleted_with_its_check(self, temp_db):
        store = SqliteStore(temp_db)
        check = Check(
            check_id=1,
            service_id=1,
            name="Cascade",
            check_type=CheckType.HTTP,
            url="https://example.test/cascade",
            data={},
        )
        await store.checks._add_async(check)
        await store.checks._set_notification_state_async(
            1, NotificationState(2, 2, 0, 100, 90)
        )

        async with aiosqlite.connect(temp_db) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("DELETE FROM health_check WHERE id = 1")
            await db.commit()

        assert (
            await store.checks._get_notification_state_async(1) == NotificationState()
        )

    @pytest.mark.anyio
    async def test_late_completion_cannot_release_newer_claim(
        self, temp_db, monkeypatch
    ):
        store = SqliteStore(temp_db)
        check = Check(
            check_id=1,
            service_id=1,
            name="Claim identity",
            check_type=CheckType.HTTP,
            url="https://example.test/claim",
            next_check_time=0,
            data={},
        )
        await store.checks._add_async(check)
        now = 10_000
        monkeypatch.setattr(
            "nyxmon.adapters.repositories.sqlite_repo.current_epoch", lambda: now
        )
        [first_claim] = await store.checks.list_due_checks_async()

        now += 301
        [reclaimed] = await store.checks.reclaim_stale_checks_async(300)
        reclaimed.next_check_time = 0
        await store.checks._add_async(reclaimed)
        now += 1
        [second_claim] = await store.checks.list_due_checks_async()
        await store.checks._set_notification_state_async(
            1, NotificationState(4, 2, 123)
        )

        first_claim.schedule_next_check()
        completion_applied = await store._persist_check_result_async(
            first_claim,
            Result(check_id=1, status=ResultStatus.OK, data={}),
            (NotificationState(4, 2, 123), NotificationState()),
        )

        assert completion_applied is False
        [persisted] = await store.checks.list_async()
        assert persisted.status == CheckStatus.PROCESSING
        assert persisted.processing_started_at == second_claim.processing_started_at
        assert await store.checks._get_notification_state_async(1) == NotificationState(
            4, 2, 123
        )
        assert len(await store.results._list_async()) == 1

    @pytest.mark.anyio
    async def test_late_completion_cannot_override_reclaimed_idle_backoff(
        self, temp_db, monkeypatch
    ):
        store = SqliteStore(temp_db)
        check = Check(
            check_id=1,
            service_id=1,
            name="Idle generation",
            check_type=CheckType.HTTP,
            url="https://example.test/idle-generation",
            next_check_time=0,
            data={},
        )
        await store.checks._add_async(check)
        now = 10_000
        monkeypatch.setattr(
            "nyxmon.adapters.repositories.sqlite_repo.current_epoch", lambda: now
        )
        [original_claim] = await store.checks.list_due_checks_async()
        now += 301
        [reclaimed] = await store.checks.reclaim_stale_checks_async(300)
        reclaimed.schedule_next_check()
        assert await store._persist_check_result_async(
            reclaimed,
            Result(check_id=1, status=ResultStatus.ERROR, data={}),
            (NotificationState(), NotificationState(0, 0, 123)),
        )
        [after_recovery] = await store.checks.list_async()

        original_claim.schedule_next_check()
        assert not await store._persist_check_result_async(
            original_claim,
            Result(check_id=1, status=ResultStatus.OK, data={}),
            (NotificationState(0, 0, 123), NotificationState()),
        )

        [persisted] = await store.checks.list_async()
        assert persisted.next_check_time == after_recovery.next_check_time
        assert await store.checks._get_notification_state_async(1) == NotificationState(
            0, 0, 123
        )
        assert len(await store.results._list_async()) == 2

    @pytest.mark.anyio
    async def test_due_check_claims_are_bounded(self, temp_db):
        store = SqliteStore(temp_db)
        for check_id in range(1, 8):
            await store.checks._add_async(
                Check(
                    check_id=check_id,
                    service_id=1,
                    name=f"Batch {check_id}",
                    check_type=CheckType.HTTP,
                    url="https://example.test/batch",
                    next_check_time=100 - check_id,
                    data={},
                )
            )

        claimed = await store.checks.list_due_checks_async()

        assert {check.check_id for check in claimed} == {7, 6, 5, 4, 3}
        assert {
            check.check_id for check in await store.checks.list_due_checks_async()
        } == {2, 1}

    @pytest.mark.anyio
    async def test_stale_reclaims_are_oldest_first_and_bounded(
        self, temp_db, monkeypatch
    ):
        store = SqliteStore(temp_db)
        for check_id in range(1, 8):
            await store.checks._add_async(
                Check(
                    check_id=check_id,
                    service_id=1,
                    name=f"Stale {check_id}",
                    check_type=CheckType.HTTP,
                    url="https://example.test/stale-batch",
                    status=CheckStatus.PROCESSING,
                    processing_started_at=100 - check_id,
                    data={},
                )
            )
        monkeypatch.setattr(
            "nyxmon.adapters.repositories.sqlite_repo.current_epoch", lambda: 1000
        )

        first = await store.checks.reclaim_stale_checks_async(300)
        second = await store.checks.reclaim_stale_checks_async(300)

        assert {check.check_id for check in first} == {7, 6, 5, 4, 3}
        assert {check.check_id for check in second} == {2, 1}

    @pytest.mark.anyio
    async def test_result_only_persistence_never_mutates_anchor_check(self, temp_db):
        store = SqliteStore(temp_db)
        current = Check(
            check_id=1,
            service_id=1,
            name="Reminder anchor",
            check_type=CheckType.HTTP,
            url="https://example.test/reminder-anchor",
            next_check_time=123,
            data={},
        )
        await store.checks._add_async(current)
        snapshot = Check(
            check_id=1,
            service_id=1,
            name="stale snapshot",
            check_type=CheckType.HTTP,
            url="https://example.test/stale",
            next_check_time=999,
            data={},
        )

        assert await store._persist_check_result_async(
            snapshot,
            Result(check_id=1, status=ResultStatus.ERROR, data={}),
            (NotificationState(), NotificationState()),
            complete_check=False,
        )

        [persisted] = await store.checks.list_async()
        assert persisted.name == "Reminder anchor"
        assert persisted.next_check_time == 123
        assert len(await store.results._list_async()) == 1

    @pytest.mark.anyio
    async def test_disabled_claim_is_released_without_late_reenable(
        self, temp_db, monkeypatch
    ):
        store = SqliteStore(temp_db)
        check = Check(
            check_id=1,
            service_id=1,
            name="Disabled in flight",
            check_type=CheckType.HTTP,
            url="https://example.test/disabled",
            next_check_time=0,
            data={},
        )
        await store.checks._add_async(check)
        now = 10_000
        monkeypatch.setattr(
            "nyxmon.adapters.repositories.sqlite_repo.current_epoch", lambda: now
        )
        [claimed] = await store.checks.list_due_checks_async()
        async with aiosqlite.connect(temp_db) as db:
            await db.execute("UPDATE health_check SET disabled = 1 WHERE id = 1")
            await db.commit()

        now += 301
        [reclaimed] = await store.checks.reclaim_stale_checks_async(300)
        assert reclaimed.disabled is True
        assert reclaimed.status == CheckStatus.IDLE

        claimed.schedule_next_check()
        await store._persist_check_result_async(
            claimed,
            Result(check_id=1, status=ResultStatus.OK, data={}),
            None,
        )
        [persisted] = await store.checks.list_async()
        assert persisted.disabled is True

    @pytest.mark.anyio
    async def test_late_completion_does_not_resurrect_deleted_check(self, temp_db):
        store = SqliteStore(temp_db)
        check = Check(
            check_id=1,
            service_id=1,
            name="Delete in flight",
            check_type=CheckType.HTTP,
            url="https://example.test/deleted",
            next_check_time=0,
            data={},
        )
        await store.checks._add_async(check)
        [claimed] = await store.checks.list_due_checks_async()
        async with aiosqlite.connect(temp_db) as db:
            await db.execute("DELETE FROM health_check WHERE id = 1")
            await db.commit()

        claimed.schedule_next_check()
        await store._persist_check_result_async(
            claimed,
            Result(check_id=1, status=ResultStatus.OK, data={}),
            None,
        )

        assert await store.checks.list_async() == []
        assert await store.results._list_async() == []

    @pytest.mark.anyio
    async def test_reclaimed_result_does_not_resurrect_deleted_check(
        self, temp_db, monkeypatch
    ):
        store = SqliteStore(temp_db)
        check = Check(
            check_id=1,
            service_id=1,
            name="Delete after reclaim",
            check_type=CheckType.HTTP,
            url="https://example.test/deleted-reclaimed",
            next_check_time=0,
            data={},
        )
        await store.checks._add_async(check)
        now = 10_000
        monkeypatch.setattr(
            "nyxmon.adapters.repositories.sqlite_repo.current_epoch", lambda: now
        )
        await store.checks.list_due_checks_async()
        now += 301
        [reclaimed] = await store.checks.reclaim_stale_checks_async(300)
        async with aiosqlite.connect(temp_db) as db:
            await db.execute("DELETE FROM health_check WHERE id = 1")
            await db.commit()

        reclaimed.schedule_next_check()
        await store._persist_check_result_async(
            reclaimed,
            Result(check_id=1, status=ResultStatus.ERROR, data={}),
            None,
        )

        assert await store.checks.list_async() == []
        assert await store.results._list_async() == []

    def test_public_persist_accepts_uri_and_multiple_results(self, temp_db):
        db_uri = f"file:{temp_db}?mode=rwc"
        store = SqliteStore(db_uri)
        portal_provider = BlockingPortalProvider()
        store.set_portal_provider(portal_provider)
        check = Check(
            check_id=1,
            service_id=1,
            name="URI persistence",
            check_type=CheckType.HTTP,
            url="https://example.test/uri",
            data={},
        )
        with portal_provider as portal:
            portal.call(store.checks._add_async, check)

        store.persist_check_result(
            check,
            Result(check_id=1, status=ResultStatus.ERROR, data={}),
            (NotificationState(), NotificationState(1, 0, 0)),
        )
        store.persist_check_result(
            check,
            Result(check_id=1, status=ResultStatus.ERROR, data={}),
            (NotificationState(1, 0, 0), NotificationState(2, 2, 0)),
        )

        with sqlite3.connect(temp_db) as db:
            rows = db.execute(
                "SELECT id, created_at FROM check_result ORDER BY id"
            ).fetchall()
        assert len(rows) == 2
        assert rows[0][0] != rows[1][0]
        assert all(row[1] for row in rows)

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
