import sqlite3
import json
import logging
from time import time as current_epoch
import datetime
import threading
from typing import Any, List, cast
import anyio
import aiosqlite

from pathlib import Path

from anyio.from_thread import BlockingPortalProvider

from ...domain import Check, Result, Service
from .interface import (
    DELIVERY_ATTEMPT_KEY,
    CollectorIncident,
    CollectorIncidentAlert,
    HeldCheck,
    NOTIFICATION_STATE_COLUMNS,
    NotificationState,
    NotificationTransition,
    RepositoryStore,
    NotificationStateConflict,
    carry_delivery_markers,
    check_batch_size,
    with_delivery_intent,
    without_delivery_markers,
    CheckRepository,
    ResultRepository,
    ServiceRepository,
)

NOTIFICATION_STATE_SELECT = ", ".join(NOTIFICATION_STATE_COLUMNS)
NOTIFICATION_STATE_PLACEHOLDERS = ", ".join(
    ["?"] * (len(NOTIFICATION_STATE_COLUMNS) + 1)
)
NOTIFICATION_STATE_ASSIGNMENTS = ",\n               ".join(
    f"{column} = excluded.{column}" for column in NOTIFICATION_STATE_COLUMNS
)

logger = logging.getLogger(__name__)


class CheckIdExistsError(Exception):
    """Raised when creating a check under an id that is already taken."""

    def __init__(self, check_id: int) -> None:
        super().__init__(f"check {check_id} already exists")
        self.check_id = check_id


def _parse_check_data(data_raw: Any) -> dict[str, Any]:
    """Decode a ``health_check.data`` column value into a dictionary.

    Args:
        data_raw: The raw column value (JSON text, bytes, dict, or ``None``).

    Returns:
        The decoded mapping, empty when the column carries no payload.
    """
    if data_raw is None:
        return {}
    if isinstance(data_raw, str):
        return json.loads(data_raw) if data_raw else {}
    if isinstance(data_raw, bytes):
        return json.loads(data_raw.decode("utf-8")) if data_raw else {}
    if isinstance(data_raw, dict):
        return data_raw
    return cast(dict[str, Any], data_raw)


def row_to_check(row: aiosqlite.Row) -> Check:
    check_id = row["id"]
    service_id = row["service_id"]
    name = row["name"]
    check_type = row["check_type"]
    url = row["url"]
    check_interval = row["check_interval"]
    next_check_time = row["next_check_time"]
    processing_started_at = row["processing_started_at"]
    status = row["status"]
    disabled = bool(row["disabled"])  # SQLite stores booleans as 0/1

    # Parse JSON data column (handle missing column gracefully for migration)
    # Note: aiosqlite.Row doesn't support .get(), use KeyError guard
    try:
        data_raw = row["data"]
    except (KeyError, IndexError):
        # Column doesn't exist yet (migration in progress)
        data: dict[str, Any] = {}
    else:
        data = _parse_check_data(data_raw)

    check = Check(
        check_id=check_id,
        service_id=service_id,
        name=name,
        check_type=check_type,
        url=url,
        check_interval=check_interval,
        next_check_time=next_check_time,
        processing_started_at=processing_started_at,
        status=status,
        disabled=disabled,
        data=data,
    )
    return check


def _row_to_collector_incident(row: Any) -> CollectorIncident | None:
    if row is None:
        return None
    try:
        payload = json.loads(row[4]) if row[4] else {}
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return CollectorIncident(
        incident_key=str(row[0]),
        opened_at=int(row[1] or 0),
        last_alert_at=int(row[2] or 0),
        alert_count=int(row[3] or 0),
        payload=payload,
    )


async def _select_notification_state(
    db: aiosqlite.Connection, check_id: int
) -> NotificationState:
    cursor = await db.execute(
        f"""SELECT {NOTIFICATION_STATE_SELECT}
            FROM check_notification_state WHERE check_id = ?""",
        (check_id,),
    )
    return NotificationState.from_row(await cursor.fetchone())


async def _upsert_notification_state(
    db: aiosqlite.Connection, check_id: int, state: NotificationState
) -> None:
    await db.execute(
        f"""INSERT INTO check_notification_state
               (check_id, {NOTIFICATION_STATE_SELECT})
           VALUES ({NOTIFICATION_STATE_PLACEHOLDERS})
           ON CONFLICT(check_id) DO UPDATE SET
               {NOTIFICATION_STATE_ASSIGNMENTS}""",
        (check_id, *state.as_row()),
    )


async def _mark_held_since(
    db: aiosqlite.Connection, check_id: int, hold_marker: int
) -> None:
    """Stamp ``held_since`` on a check's state row unless it is already held.

    Runs on the caller's open transaction so the hold commits together with the
    completion that produced it.

    Args:
        db: The connection whose transaction the write joins.
        check_id: The check that owes a recheck.
        hold_marker: Epoch of the first held sample of this hold.
    """
    cursor = await db.execute(
        """UPDATE check_notification_state SET held_since = ?
           WHERE check_id = ? AND held_since = 0""",
        (hold_marker, check_id),
    )
    if cursor.rowcount:
        return
    existing = await db.execute(
        "SELECT 1 FROM check_notification_state WHERE check_id = ?", (check_id,)
    )
    if await existing.fetchone() is not None:
        # A hold is already running; its start time is the one that bounds it.
        return
    await db.execute(
        f"""INSERT INTO check_notification_state (check_id, {NOTIFICATION_STATE_SELECT})
            VALUES ({NOTIFICATION_STATE_PLACEHOLDERS})""",
        (check_id, *NotificationState(held_since=hold_marker).as_row()),
    )


NOTIFICATION_STATE_UPGRADE_COLUMNS = (
    "last_notified_at",
    "first_failure_at",
    "held_since",
    "attempt_seq",
    "attempt_at",
)

# Only these two need a backfill; the site-connectivity columns are meaningful
# at their zero default, so adding them alone must not claim an adoption.
NOTIFICATION_STATE_BACKFILLED_COLUMNS = frozenset(
    {"last_notified_at", "first_failure_at"}
)


async def _upgrade_notification_state_schema(db: aiosqlite.Connection) -> None:
    """Add the reminder and site-connectivity columns to an older state table.

    ``CREATE TABLE IF NOT EXISTS`` silently leaves an older table untouched, so
    the new columns are added here.

    The column additions and their backfill MUST commit atomically. The backfill
    only runs for columns this call actually added, because an existing column
    is indistinguishable from one that was added and backfilled earlier. If the
    process died between the ``ALTER TABLE`` and its ``UPDATE``, the next run
    would see the column already present, drop it from ``added``, and skip the
    adoption permanently - leaving every already-alerting streak at
    ``last_notified_at = 0`` so the rollout re-pages all of them. That is exactly
    the alert storm this schema exists to prevent, so the whole upgrade runs in
    one explicit transaction: either the columns and their backfill both land,
    or neither does and the next run retries cleanly.

    SQLite makes DDL transactional, and it serialises writers, so this is also
    safe to race with concurrent result persistence.
    """
    await db.execute("BEGIN IMMEDIATE")
    try:
        added: list[str] = []
        for column in NOTIFICATION_STATE_UPGRADE_COLUMNS:
            try:
                await db.execute(
                    f"ALTER TABLE check_notification_state "
                    f"ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
            else:
                added.append(column)

        if not added:
            await db.rollback()
            return

        backfilled = NOTIFICATION_STATE_BACKFILLED_COLUMNS.intersection(added)
        bootstrap_epoch = int(current_epoch())
        if "first_failure_at" in added:
            await db.execute(
                "UPDATE check_notification_state SET first_failure_at = ? "
                "WHERE failure_count > 0 AND first_failure_at = 0",
                (bootstrap_epoch,),
            )
        if "last_notified_at" in added:
            # Streaks that already alerted are adopted as ongoing incidents so
            # the rollout does not re-page them; streaks that never reached the
            # alert threshold keep last_notified_at = 0 and follow the normal
            # threshold.
            await db.execute(
                "UPDATE check_notification_state SET last_notified_at = ? "
                "WHERE failure_count > 0 AND last_attempt_count > 0 "
                "AND last_notified_at = 0",
                (bootstrap_epoch,),
            )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    if backfilled:
        logger.info(
            "upgraded check_notification_state with columns %s; "
            "existing failure streaks adopted at epoch %s without re-alerting",
            ", ".join(added),
            bootstrap_epoch,
        )
    else:
        logger.info(
            "upgraded check_notification_state with columns %s",
            ", ".join(added),
        )


class SqliteCheckRepository(CheckRepository):
    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        self._use_uri = self._db_path.startswith("file:")
        self._portal_provider: BlockingPortalProvider | None = None
        self._schema_ready = False
        self.seen: set[Check] = set()

    # ---------- öffentliche, SYNCHRONE Ports ----------
    def get(self, check_id: int) -> Check:
        """Get a check from the repository by ID."""
        return self._await(self._get_async(check_id))

    def list(self) -> List[Check]:
        return self._await(self.list_async())

    def add(self, check: Check) -> None:
        if self._portal_provider is None:
            return  # No portal provider set, cannot add check
        with self._portal_provider as portal:
            portal.call(self._add_async, check)

    # ---------- interne async-Implementierung ----------
    async def _get_async(self, check_id: int) -> Check:
        """Get a check from the repository by ID asynchronously."""
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            db.row_factory = aiosqlite.Row
            [row] = await db.execute_fetchall(
                "SELECT id, service_id, name, check_type, url, check_interval, next_check_time, processing_started_at, status, disabled, data FROM health_check WHERE id = ?",
                (check_id,),
            )
            if row is None:
                raise KeyError(f"Check with ID {check_id} not found")
            return row_to_check(row)

    async def list_async(self) -> List[Check]:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)

            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT id, service_id, name, check_type, url, check_interval, next_check_time, processing_started_at, status, disabled, data FROM health_check"
            )
            return [row_to_check(r) for r in rows]

    async def list_due_checks_async(self) -> List[Check]:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)

            current_time = int(current_epoch())
            db.row_factory = aiosqlite.Row

            # Single atomic operation to find and claim checks
            # Using SQLite's RETURNING clause (available in SQLite 3.35.0+)
            result = await db.execute(
                """UPDATE health_check
                   SET status                = 'processing',
                       processing_started_at = ?
                   WHERE id IN (SELECT id
                                FROM health_check
                                WHERE next_check_time <= ?
                                  AND status = 'idle'
                                  AND disabled = 0
                                ORDER BY next_check_time ASC, id ASC
                                LIMIT ?
                   )
                   RETURNING id, service_id, name, check_type, url, check_interval, next_check_time, processing_started_at, status, disabled, data""",
                (current_time, current_time, check_batch_size()),
            )

            rows = await result.fetchall()
            await db.commit()

            return [row_to_check(r) for r in rows]

    async def reclaim_stale_checks_async(self, lease_seconds: int) -> List[Check]:
        """Atomically release checks abandoned by an interrupted worker."""
        current_time = int(current_epoch())
        stale_before = current_time - lease_seconds
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            db.row_factory = aiosqlite.Row
            candidate = await db.execute_fetchall(
                """SELECT 1 FROM health_check
                   WHERE status = 'processing'
                     AND (COALESCE(processing_started_at, 0) = 0
                          OR processing_started_at <= ?)
                   LIMIT 1""",
                (stale_before,),
            )
            if not candidate:
                return []
            # Legacy or malformed claims without a timestamp receive a full lease
            # from first observation instead of being reclaimed immediately.
            await db.execute(
                """UPDATE health_check
                   SET processing_started_at = ?
                   WHERE status = 'processing'
                     AND COALESCE(processing_started_at, 0) = 0""",
                (current_time,),
            )
            result = await db.execute(
                """UPDATE health_check
                   SET status = 'idle', processing_started_at = 0
                   WHERE id IN (
                       SELECT id FROM health_check
                       WHERE status = 'processing'
                         AND processing_started_at > 0
                         AND processing_started_at <= ?
                       ORDER BY processing_started_at ASC, id ASC
                       LIMIT ?
                   )
                   RETURNING id, service_id, name, check_type, url, check_interval,
                             next_check_time, processing_started_at, status, disabled, data""",
                (stale_before, check_batch_size()),
            )
            rows = await result.fetchall()
            await db.commit()
            return [row_to_check(row) for row in rows]

    def get_notification_state(self, check_id: int) -> NotificationState:
        if self._portal_provider is None:
            raise RuntimeError("portal provider is required for notification state")
        with self._portal_provider as portal:
            return portal.call(self._get_notification_state_async, check_id)

    async def _get_notification_state_async(self, check_id: int) -> NotificationState:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            return await _select_notification_state(db, check_id)

    def set_notification_state(self, check_id: int, state: NotificationState) -> None:
        if self._portal_provider is None:
            raise RuntimeError("portal provider is required for notification state")
        with self._portal_provider as portal:
            portal.call(self._set_notification_state_async, check_id, state)

    async def _set_notification_state_async(
        self, check_id: int, state: NotificationState
    ) -> None:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            await _upsert_notification_state(db, check_id, state)
            await db.commit()

    def acknowledge_notification_attempt(self, check_id: int, attempt_seq: int) -> bool:
        """Clear the pending delivery marker of one notification attempt."""
        if self._portal_provider is None:
            raise RuntimeError("portal provider is required for notification state")
        with self._portal_provider as portal:
            return portal.call(
                self._acknowledge_notification_attempt_async, check_id, attempt_seq
            )

    async def _acknowledge_notification_attempt_async(
        self, check_id: int, attempt_seq: int
    ) -> bool:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            cursor = await db.execute(
                """UPDATE check_notification_state SET attempt_at = 0
                   WHERE check_id = ? AND attempt_seq = ? AND attempt_at <> 0""",
                (check_id, attempt_seq),
            )
            await db.commit()
            return bool(cursor.rowcount)

    async def list_held_checks_async(self) -> List[HeldCheck]:
        """Return every check whose notification state has ``held_since > 0``."""
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            cursor = await db.execute(
                """SELECT hc.id, hc.data, hc.status, hc.disabled, hc.next_check_time,
                          ns.held_since
                   FROM check_notification_state AS ns
                   JOIN health_check AS hc ON hc.id = ns.check_id
                   WHERE ns.held_since > 0
                   ORDER BY hc.id ASC"""
            )
            rows = await cursor.fetchall()
            return [
                HeldCheck(
                    check_id=int(row[0]),
                    data=_parse_check_data(row[1]),
                    status=str(row[2] or ""),
                    disabled=bool(row[3]),
                    next_check_time=int(row[4] or 0),
                    held_since=int(row[5] or 0),
                )
                for row in rows
            ]

    def count_held_checks(self) -> int:
        """Return the number of checks with ``held_since > 0``."""
        if self._portal_provider is None:
            raise RuntimeError("portal provider is required for notification state")
        with self._portal_provider as portal:
            return portal.call(self.count_held_checks_async)

    async def count_held_checks_async(self) -> int:
        """Return the number of checks with ``held_since > 0``."""
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            cursor = await db.execute(
                "SELECT COUNT(*) FROM check_notification_state WHERE held_since > 0"
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def count_processing_claims_before_async(self, epoch: int) -> int:
        """Count executions claimed before ``epoch`` that are still in flight."""
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            cursor = await db.execute(
                """SELECT COUNT(*) FROM health_check
                   WHERE status = 'processing'
                     AND processing_started_at > 0
                     AND processing_started_at < ?""",
                (epoch,),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def reschedule_checks_async(
        self, check_ids: List[int], *, run_at: int
    ) -> int:
        """Pull idle, enabled checks forward to ``run_at``."""
        if not check_ids:
            return 0
        placeholders = ",".join(["?"] * len(check_ids))
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            cursor = await db.execute(
                f"""UPDATE health_check SET next_check_time = ?
                    WHERE id IN ({placeholders})
                      AND status = 'idle'
                      AND disabled = 0
                      AND next_check_time > ?""",
                (run_at, *check_ids, run_at),
            )
            await db.commit()
            return int(cursor.rowcount or 0)

    async def _add_async(self, check: Check) -> None:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            await self._upsert_on_connection(db, check)
            await db.commit()

    async def create_async(
        self, check: Check, *, check_id: int | None = None, replace: bool = False
    ) -> int:
        """Store ``check`` as a new row and return the id it was stored under.

        Unlike :meth:`add`, which upserts by ``check.check_id``, this never
        overwrites an existing check by accident.

        Args:
            check: The check to store. Its ``check_id`` is ignored and set to
                the id that was actually used.
            check_id: Store under this id. ``None`` lets SQLite assign the next
                free id.
            replace: When ``check_id`` names an existing check, overwrite it.
                Without it an existing id raises :class:`CheckIdExistsError`.

        Returns:
            The id of the stored check.
        """
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            # Take the write lock before reading, so the existence check and
            # the insert cannot interleave with another writer.
            await db.execute("BEGIN IMMEDIATE")
            try:
                if check_id is None:
                    cursor = await db.execute(
                        """INSERT INTO health_check
                           (service_id, name, check_type, url, check_interval,
                            status, next_check_time, processing_started_at,
                            disabled, data)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            check.service_id,
                            check.name,
                            check.check_type,
                            check.url,
                            check.check_interval,
                            check.status,
                            check.next_check_time,
                            check.processing_started_at,
                            int(check.disabled),
                            json.dumps(check.data),
                        ),
                    )
                    if cursor.lastrowid is None:  # pragma: no cover - defensive
                        raise RuntimeError("SQLite did not report the new check id")
                    check.check_id = int(cursor.lastrowid)
                else:
                    cursor = await db.execute(
                        "SELECT 1 FROM health_check WHERE id = ?", (check_id,)
                    )
                    exists = await cursor.fetchone() is not None
                    if exists and not replace:
                        raise CheckIdExistsError(check_id)
                    check.check_id = check_id
                    await self._upsert_on_connection(db, check)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return check.check_id

    @staticmethod
    async def _upsert_on_connection(db: aiosqlite.Connection, check: Check) -> None:
        await db.execute(
            """INSERT INTO health_check
               (id, service_id, name, check_type, url, check_interval,
                status, next_check_time, processing_started_at, disabled, data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   service_id = excluded.service_id,
                   name = excluded.name,
                   check_type = excluded.check_type,
                   url = excluded.url,
                   check_interval = excluded.check_interval,
                   status = excluded.status,
                   next_check_time = excluded.next_check_time,
                   processing_started_at = excluded.processing_started_at,
                   disabled = excluded.disabled,
                   data = excluded.data""",
            (
                check.check_id,
                check.service_id,
                check.name,
                check.check_type,
                check.url,
                check.check_interval,
                check.status,
                check.next_check_time,
                check.processing_started_at,
                int(check.disabled),
                json.dumps(check.data),
            ),
        )

    @staticmethod
    async def _complete_on_connection(db: aiosqlite.Connection, check: Check) -> bool:
        cursor = await db.execute(
            """UPDATE health_check
               SET status = ?, next_check_time = ?, processing_started_at = ?
               WHERE id = ?
                 AND ((status = 'processing' AND processing_started_at = ?)
                      OR (status <> 'processing' AND ? = 0))""",
            (
                check.status,
                check.next_check_time,
                check.processing_started_at,
                check.check_id,
                check.claim_started_at,
                check.claim_started_at,
            ),
        )
        return bool(cursor.rowcount)

    # ---------- Bridge sync → async ----------
    def _await(self, coro):
        async def _run():
            return await coro  # Coroutine tatsächlich ausführen

        return anyio.from_thread.run(_run)  # Callable (!) an from_thread.run()

    # ---------- einmalige Schema-Initialisierung ----------

    async def _ensure_schema(self, db: aiosqlite.Connection) -> None:
        if self._schema_ready:
            return

        # Create table if not exists
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS health_check (
                id               INTEGER PRIMARY KEY,
                service_id       INTEGER NOT NULL,
                name             TEXT    DEFAULT '',
                check_type       TEXT    NOT NULL,
                url              TEXT    NOT NULL,
                check_interval   INTEGER NOT NULL,
                status           TEXT    DEFAULT 'idle',
                next_check_time  INTEGER DEFAULT 0,
                processing_started_at INTEGER DEFAULT 0,
                disabled         INTEGER DEFAULT 0,
                data             TEXT    DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS check_notification_state (
                check_id           INTEGER PRIMARY KEY
                                   REFERENCES health_check(id) ON DELETE CASCADE,
                failure_count      INTEGER NOT NULL DEFAULT 0,
                last_attempt_count INTEGER NOT NULL DEFAULT 0,
                last_immediate_at  INTEGER NOT NULL DEFAULT 0,
                last_notified_at   INTEGER NOT NULL DEFAULT 0,
                first_failure_at   INTEGER NOT NULL DEFAULT 0,
                held_since         INTEGER NOT NULL DEFAULT 0,
                attempt_seq        INTEGER NOT NULL DEFAULT 0,
                attempt_at         INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS collector_incident (
                incident_key  TEXT    PRIMARY KEY,
                opened_at     INTEGER NOT NULL DEFAULT 0,
                last_alert_at INTEGER NOT NULL DEFAULT 0,
                alert_count   INTEGER NOT NULL DEFAULT 0,
                payload       TEXT    NOT NULL DEFAULT '{}'
            );
            """
        )

        await _upgrade_notification_state_schema(db)

        # Add data column to existing tables (idempotent migration)
        # This handles databases created before data column was added
        try:
            await db.execute(
                """ALTER TABLE health_check ADD COLUMN data TEXT DEFAULT '{}'"""
            )
        except sqlite3.OperationalError as e:
            # Column already exists, ignore
            if "duplicate column name" not in str(e).lower():
                raise

        await db.commit()
        self._schema_ready = True


class SqliteResultRepository(ResultRepository):
    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        self._use_uri = self._db_path.startswith("file:")
        self._schema_ready = False
        self.seen = set()
        self._portal_provider: BlockingPortalProvider | None = None

    # ---------- öffentliche, SYNCHRONE Ports ----------
    def add(self, result: Result) -> None:
        if self._portal_provider is None:
            return  # No portal provider set, cannot add a result
        with self._portal_provider as portal:
            portal.call(self._add_async, result)

    def get(self, result_id: int) -> Result:
        return self._await(self._get_async(result_id))

    def list(self) -> List[Result]:
        return self._await(self._list_async())

    def list_for_check(self, check_id: int, limit: int) -> List[Result]:
        return self._await(self._list_for_check_async(check_id, limit))

    # ---------- interne async-Implementierung ----------
    async def _add_async(self, result: Result) -> None:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            await self._insert_on_connection(db, result)
            await db.commit()
            self.seen.add(result)

    @staticmethod
    async def _insert_on_connection(db: aiosqlite.Connection, result: Result) -> None:
        await db.execute(
            """INSERT INTO check_result
                   (id, health_check_id, status, data, created_at)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            (
                result.result_id,
                result.check_id,
                result.status,
                json.dumps(result.data),
            ),
        )

    async def _get_async(self, result_id: int) -> Result:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            db.row_factory = aiosqlite.Row
            [row] = await db.execute_fetchall(
                "SELECT id, health_check_id, status, data FROM check_result WHERE id = ?",
                (result_id,),
            )
            if row is None:
                raise KeyError(f"Result with ID {result_id} not found")
            return Result(
                result_id=row["id"],
                check_id=row["check_id"],
                status=row["status"],
                data=json.loads(row["data"]),
            )

    async def _list_async(self) -> List[Result]:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT id, health_check_id, status, data FROM check_result"
            )
            return [
                Result(
                    result_id=row["id"],
                    check_id=row["health_check_id"],
                    status=row["status"],
                    data=json.loads(row["data"]),
                )
                for row in rows
            ]

    async def _list_for_check_async(self, check_id: int, limit: int) -> List[Result]:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                """
                SELECT id, health_check_id, status, data
                FROM check_result
                WHERE health_check_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (check_id, limit),
            )
            return [
                Result(
                    result_id=row["id"],
                    check_id=row["health_check_id"],
                    status=row["status"],
                    data=json.loads(row["data"] or "{}"),
                )
                for row in rows
            ]

    async def delete_old_results_async(
        self, retention_seconds: int = 86400, batch_size: int = 1000
    ) -> int:
        """Delete check results older than the specified period."""
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)

            # Calculate the cutoff timestamp (SQLite timestamp format)
            cutoff_time = datetime.datetime.now() - datetime.timedelta(
                seconds=retention_seconds
            )
            cutoff_time_str = cutoff_time.strftime("%Y-%m-%d %H:%M:%S")

            # First, get the IDs to delete (with limit)
            # SQLite doesn't support LIMIT in DELETE directly, so we need to do this in two steps
            cursor = await db.execute(
                "SELECT id FROM check_result WHERE created_at < ? ORDER BY id LIMIT ?",
                (cutoff_time_str, batch_size),
            )
            rows = await cursor.fetchall()

            if not rows:
                return 0

            # Get the IDs to delete as a tuple
            ids_to_delete = tuple(row[0] for row in rows)

            # If only one ID to delete, we need special SQL syntax (can't use 'IN' with a single value tuple)
            if len(ids_to_delete) == 1:
                delete_sql = "DELETE FROM check_result WHERE id = ?"
                params = (ids_to_delete[0],)
            else:
                placeholders = ",".join(["?"] * len(ids_to_delete))
                delete_sql = f"DELETE FROM check_result WHERE id IN ({placeholders})"
                params = ids_to_delete

            # Delete the records
            cursor = await db.execute(delete_sql, params)
            deleted_count = cursor.rowcount
            await db.commit()

            return deleted_count

    def delete_old_results(
        self, retention_seconds: int = 86400, batch_size: int = 1000
    ) -> int:
        """Delete check results older than the specified period."""
        return self._await(
            self.delete_old_results_async(
                retention_seconds=retention_seconds, batch_size=batch_size
            )
        )

    # ---------- Bridge sync → async ----------
    def _await(self, coro):
        async def _run():
            return await coro

        return anyio.from_thread.run(_run)

    # ---------- einmalige Schema-Initialisierung ----------
    async def _ensure_schema(self, db: aiosqlite.Connection) -> None:
        if self._schema_ready:
            return
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS check_result (
                id              INTEGER PRIMARY KEY,
                health_check_id INTEGER NOT NULL,
                status          TEXT NOT NULL,
                data            TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await db.commit()
        self._schema_ready = True


class SqliteServiceRepository(ServiceRepository):
    def __init__(self, db_path: Path | str) -> None:
        self._db_path = db_path
        self._use_uri = str(db_path).startswith("file:")
        self._portal_provider: BlockingPortalProvider | None = None
        self._schema_ready = False
        self.seen: set[Service] = set()

    # ---------- öffentliche, SYNCHRONE Ports ----------
    def list(self) -> List[Service]:
        return self._await(self.list_async())

    def add(self, service: Service) -> None:
        if self._portal_provider is None:
            return  # No portal provider set, cannot add service
        with self._portal_provider as portal:
            portal.call(self._add_async, service)

    def get(self, service_id: int) -> Service:
        return self._await(self._get_async(service_id))

    # ---------- interne async-Implementierung ----------
    async def list_async(self) -> List[Service]:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)

            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall("SELECT id, name FROM service")
            services = []
            for row in rows:
                service_id, name = row
                data = {"name": name}
                services.append(Service(service_id=service_id, data=data))
            return services

    async def _add_async(self, service: Service) -> None:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)

            await db.execute(
                """INSERT OR REPLACE INTO service (id, name) VALUES (?, ?)""",
                (
                    service.service_id,
                    service.data.get("name", ""),
                ),
            )
            await db.commit()
            self.seen.add(service)

    async def _get_async(self, service_id: int) -> Service:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)

            db.row_factory = aiosqlite.Row
            [row] = await db.execute_fetchall(
                "SELECT id, name FROM service WHERE id = ?", (service_id,)
            )
            if row is None:
                raise KeyError(f"Service with ID {service_id} not found")

            service_id, name = row
            data = {"name": name}
            return Service(service_id=service_id, data=data)

    # ---------- Bridge sync → async ----------
    def _await(self, coro):
        async def _run():
            return await coro  # Coroutine tatsächlich ausführen

        return anyio.from_thread.run(_run)  # Callable (!) an from_thread.run()

    # ---------- einmalige Schema-Initialisierung ----------
    async def _ensure_schema(self, db: aiosqlite.Connection) -> None:
        if self._schema_ready:
            return
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS service
            (
                id   INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            """
        )
        await db.commit()
        self._schema_ready = True


class SqliteStore(RepositoryStore):
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = db_path
        self._use_uri = str(db_path).startswith("file:")
        self._connection_state = threading.local()

        # Initialize repositories
        self.results = SqliteResultRepository(db_path)
        self.checks = SqliteCheckRepository(db_path)
        self.services = SqliteServiceRepository(db_path)

        # Blocking portal provider
        self._portal_provider: BlockingPortalProvider | None = None

    def set_portal_provider(self, portal_provider: BlockingPortalProvider) -> None:
        """Set the portal provider for the store."""
        self._portal_provider = portal_provider
        self.results._portal_provider = portal_provider
        self.checks._portal_provider = portal_provider
        self.services._portal_provider = portal_provider

    def fork_for_concurrent_uow(self) -> "SqliteStore":
        """Use independent repositories and event sets over the same database."""
        store = SqliteStore(self.db_path)
        if self._portal_provider is not None:
            store.set_portal_provider(self._portal_provider)
        return store

    def persist_check_result(
        self,
        check: Check,
        result: Result,
        notification_transition: NotificationTransition | None,
        *,
        complete_check: bool = True,
        hold_marker: int | None = None,
    ) -> bool:
        if self._portal_provider is None:
            raise RuntimeError("portal provider is required to persist check results")
        with self._portal_provider as portal:
            return portal.call(
                self._persist_check_result_async,
                check,
                result,
                notification_transition,
                complete_check,
                hold_marker,
            )

    async def _persist_check_result_async(
        self,
        check: Check,
        result: Result,
        notification_transition: NotificationTransition | None,
        complete_check: bool = True,
        hold_marker: int | None = None,
    ) -> bool:
        async with aiosqlite.connect(self.db_path, uri=self._use_uri) as db:
            await self.checks._ensure_schema(db)
            await self.results._ensure_schema(db)
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            completion_applied = True
            if complete_check:
                completion_applied = await self.checks._complete_on_connection(
                    db, check
                )
            if not complete_check or not completion_applied:
                exists = await db.execute(
                    "SELECT 1 FROM health_check WHERE id = ?", (check.check_id,)
                )
                if await exists.fetchone() is None:
                    await db.rollback()
                    logger.info(
                        "dropped late result for deleted check_id=%s",
                        check.check_id,
                    )
                    return False
                if complete_check:
                    logger.info(
                        "stored late result without changing newer claim for check_id=%s",
                        check.check_id,
                    )
            await self.results._insert_on_connection(db, result)
            if complete_check and not completion_applied:
                await db.commit()
                self.results.seen.add(result)
                return False
            if notification_transition is not None:
                expected_state, notification_state = notification_transition
                current_state = await _select_notification_state(db, check.check_id)
                if current_state != expected_state:
                    await db.rollback()
                    raise NotificationStateConflict(check.check_id)
                if notification_state != expected_state:
                    await _upsert_notification_state(
                        db, check.check_id, notification_state
                    )
            elif hold_marker is not None:
                # Conflict-exhaustion fallback: the hold must land in the same
                # transaction as the completion, or the observer could see an
                # idle, unheld row for a claim that owes a recheck.
                await _mark_held_since(db, check.check_id, hold_marker)
            await db.commit()
            if complete_check:
                self.checks.seen.add(check)
            self.results.seen.add(result)
            return True

    # ---------- collector-level incidents ----------
    def get_collector_incident(self, incident_key: str) -> CollectorIncident | None:
        if self._portal_provider is None:
            raise RuntimeError("portal provider is required for collector incidents")
        with self._portal_provider as portal:
            return portal.call(self._get_collector_incident_async, incident_key)

    async def _get_collector_incident_async(
        self, incident_key: str
    ) -> CollectorIncident | None:
        async with aiosqlite.connect(self.db_path, uri=self._use_uri) as db:
            await self.checks._ensure_schema(db)
            cursor = await db.execute(
                """SELECT incident_key, opened_at, last_alert_at, alert_count, payload
                   FROM collector_incident WHERE incident_key = ?""",
                (incident_key,),
            )
            return _row_to_collector_incident(await cursor.fetchone())

    def claim_collector_incident_alert(
        self,
        incident_key: str,
        *,
        now: int,
        reminder_seconds: int,
        payload: dict[str, Any] | None = None,
    ) -> CollectorIncidentAlert:
        if self._portal_provider is None:
            raise RuntimeError("portal provider is required for collector incidents")
        with self._portal_provider as portal:
            return portal.call(
                self._claim_collector_incident_alert_async,
                incident_key,
                now,
                reminder_seconds,
                payload,
            )

    async def _claim_collector_incident_alert_async(
        self,
        incident_key: str,
        now: int,
        reminder_seconds: int,
        payload: dict[str, Any] | None = None,
    ) -> CollectorIncidentAlert:
        async with aiosqlite.connect(self.db_path, uri=self._use_uri) as db:
            await self.checks._ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """SELECT incident_key, opened_at, last_alert_at, alert_count, payload
                   FROM collector_incident WHERE incident_key = ?""",
                (incident_key,),
            )
            existing = _row_to_collector_incident(await cursor.fetchone())
            if existing is None:
                incident = CollectorIncident(
                    incident_key=incident_key,
                    opened_at=now,
                    last_alert_at=now,
                    alert_count=1,
                    payload=with_delivery_intent(dict(payload or {}), 1),
                )
                should_notify = True
                is_new = True
            else:
                should_notify = now - existing.last_alert_at >= max(1, reminder_seconds)
                alert_count = existing.alert_count + (1 if should_notify else 0)
                next_payload = (
                    dict(payload) if payload is not None else dict(existing.payload)
                )
                incident = CollectorIncident(
                    incident_key=incident_key,
                    opened_at=existing.opened_at,
                    last_alert_at=now if should_notify else existing.last_alert_at,
                    alert_count=alert_count,
                    payload=(
                        with_delivery_intent(next_payload, alert_count)
                        if should_notify
                        else carry_delivery_markers(next_payload, existing.payload)
                    ),
                )
                is_new = False
            await db.execute(
                """INSERT INTO collector_incident
                       (incident_key, opened_at, last_alert_at, alert_count, payload)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(incident_key) DO UPDATE SET
                       last_alert_at = excluded.last_alert_at,
                       alert_count = excluded.alert_count,
                       payload = excluded.payload""",
                (
                    incident.incident_key,
                    incident.opened_at,
                    incident.last_alert_at,
                    incident.alert_count,
                    json.dumps(incident.payload),
                ),
            )
            await db.commit()
            return CollectorIncidentAlert(
                incident=incident, should_notify=should_notify, is_new=is_new
            )

    def open_collector_incident(
        self, incident_key: str, *, now: int, payload: dict[str, Any]
    ) -> CollectorIncident:
        if self._portal_provider is None:
            raise RuntimeError("portal provider is required for collector incidents")
        with self._portal_provider as portal:
            return portal.call(
                self._open_collector_incident_async, incident_key, now, payload
            )

    async def _open_collector_incident_async(
        self, incident_key: str, now: int, payload: dict[str, Any]
    ) -> CollectorIncident:
        """Create an incident without claiming an alert, or replace its payload."""
        async with aiosqlite.connect(self.db_path, uri=self._use_uri) as db:
            await self.checks._ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """SELECT incident_key, opened_at, last_alert_at, alert_count, payload
                   FROM collector_incident WHERE incident_key = ?""",
                (incident_key,),
            )
            existing = _row_to_collector_incident(await cursor.fetchone())
            if existing is None:
                incident = CollectorIncident(
                    incident_key=incident_key,
                    opened_at=now,
                    last_alert_at=0,
                    alert_count=0,
                    payload=dict(payload),
                )
                await db.execute(
                    """INSERT INTO collector_incident
                           (incident_key, opened_at, last_alert_at, alert_count,
                            payload)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        incident.incident_key,
                        incident.opened_at,
                        incident.last_alert_at,
                        incident.alert_count,
                        json.dumps(incident.payload),
                    ),
                )
            else:
                incident = CollectorIncident(
                    incident_key=existing.incident_key,
                    opened_at=existing.opened_at,
                    last_alert_at=existing.last_alert_at,
                    alert_count=existing.alert_count,
                    payload=carry_delivery_markers(dict(payload), existing.payload),
                )
                await db.execute(
                    "UPDATE collector_incident SET payload = ? WHERE incident_key = ?",
                    (json.dumps(incident.payload), incident_key),
                )
            await db.commit()
            return incident

    def acknowledge_collector_incident_delivery(
        self, incident_key: str, attempt: int
    ) -> bool:
        if self._portal_provider is None:
            raise RuntimeError("portal provider is required for collector incidents")
        with self._portal_provider as portal:
            return portal.call(
                self._acknowledge_collector_incident_delivery_async,
                incident_key,
                attempt,
            )

    async def _acknowledge_collector_incident_delivery_async(
        self, incident_key: str, attempt: int
    ) -> bool:
        """Clear the delivery markers of ``attempt`` in one write transaction.

        The compare and the write share a single ``BEGIN IMMEDIATE``
        transaction, so a claim granted concurrently either loses the race for
        the write lock and rewrites its own intent afterwards, or wins it and
        leaves a ``delivery_attempt`` this acknowledgement no longer matches.
        """
        async with aiosqlite.connect(self.db_path, uri=self._use_uri) as db:
            await self.checks._ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """SELECT incident_key, opened_at, last_alert_at, alert_count, payload
                   FROM collector_incident WHERE incident_key = ?""",
                (incident_key,),
            )
            existing = _row_to_collector_incident(await cursor.fetchone())
            if (
                existing is None
                or existing.payload.get(DELIVERY_ATTEMPT_KEY) != attempt
            ):
                await db.commit()
                return False
            payload = without_delivery_markers(existing.payload)
            await db.execute(
                "UPDATE collector_incident SET payload = ? WHERE incident_key = ?",
                (json.dumps(payload), incident_key),
            )
            await db.commit()
            return True

    def close_collector_incident(self, incident_key: str) -> CollectorIncident | None:
        if self._portal_provider is None:
            raise RuntimeError("portal provider is required for collector incidents")
        with self._portal_provider as portal:
            return portal.call(self._close_collector_incident_async, incident_key)

    def set_collector_incident_payload(
        self, incident_key: str, payload: dict[str, Any]
    ) -> CollectorIncident | None:
        if self._portal_provider is None:
            raise RuntimeError("portal provider is required for collector incidents")
        with self._portal_provider as portal:
            return portal.call(
                self._set_collector_incident_payload_async, incident_key, payload
            )

    async def _set_collector_incident_payload_async(
        self, incident_key: str, payload: dict[str, Any]
    ) -> CollectorIncident | None:
        """Persist an open incident's payload without touching alert cadence."""
        async with aiosqlite.connect(self.db_path, uri=self._use_uri) as db:
            await self.checks._ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """SELECT incident_key, opened_at, last_alert_at, alert_count, payload
                   FROM collector_incident WHERE incident_key = ?""",
                (incident_key,),
            )
            existing = _row_to_collector_incident(await cursor.fetchone())
            if existing is None:
                await db.commit()
                return None
            await db.execute(
                "UPDATE collector_incident SET payload = ? WHERE incident_key = ?",
                (json.dumps(dict(payload)), incident_key),
            )
            await db.commit()
            return CollectorIncident(
                incident_key=existing.incident_key,
                opened_at=existing.opened_at,
                last_alert_at=existing.last_alert_at,
                alert_count=existing.alert_count,
                payload=dict(payload),
            )

    async def _close_collector_incident_async(
        self, incident_key: str
    ) -> CollectorIncident | None:
        async with aiosqlite.connect(self.db_path, uri=self._use_uri) as db:
            await self.checks._ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """SELECT incident_key, opened_at, last_alert_at, alert_count, payload
                   FROM collector_incident WHERE incident_key = ?""",
                (incident_key,),
            )
            existing = _row_to_collector_incident(await cursor.fetchone())
            if existing is not None:
                await db.execute(
                    "DELETE FROM collector_incident WHERE incident_key = ?",
                    (incident_key,),
                )
            await db.commit()
            return existing

    @property
    def connection(self) -> sqlite3.Connection:
        connection = getattr(self._connection_state, "connection", None)
        if connection is None:
            conn = sqlite3.connect(self.db_path, uri=self._use_uri)
            conn.row_factory = sqlite3.Row
            self._connection_state.connection = conn
            connection = conn
            logger.debug(
                "Created new SQLite connection for thread %s",
                threading.get_ident(),
            )
        return connection

    def list(self) -> List:
        return [
            self.results,
            self.checks,
            self.services,
        ]
