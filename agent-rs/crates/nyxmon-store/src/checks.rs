//! Check scheduling: claims, stale reclaim, release, holds
//! (`SqliteCheckRepository`; PLAN.md section 3.4).

use nyxmon_core::model::{Check, HeldCheck, NotificationState};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params, params_from_iter};

use crate::actor::Store;
use crate::error::StoreError;
use crate::rows::{
    CHECK_COLUMNS, decode_check_data, int_or_zero, row_to_check, select_notification_state,
    text_or_empty, truthy,
};

/// `list_async`: every check, in table order.
pub(crate) fn list_checks(connection: &Connection) -> Result<Vec<Check>, StoreError> {
    let mut statement = connection.prepare(&format!("SELECT {CHECK_COLUMNS} FROM health_check"))?;
    let rows = statement.query_map([], row_to_check)?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

/// One check by id, from a read-only connection (`run-check`, PLAN.md 7.4):
/// no lock, no write, the row as the claim would load it.
pub fn read_check(path: &std::path::Path, check_id: i64) -> Result<Option<Check>, StoreError> {
    let connection = crate::actor::open_read_only(path)?;
    let check = connection
        .query_row(
            &format!("SELECT {CHECK_COLUMNS} FROM health_check WHERE id = ?"),
            params![check_id],
            row_to_check,
        )
        .optional()?;
    Ok(check)
}

/// `list_due_checks_async`: atomically claim up to `batch_size` due checks.
pub(crate) fn claim_due_checks(
    connection: &mut Connection,
    now: i64,
    batch_size: i64,
) -> Result<Vec<Check>, StoreError> {
    let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let claimed = {
        let mut statement = transaction.prepare(&format!(
            "UPDATE health_check
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
                RETURNING {CHECK_COLUMNS}"
        ))?;
        let rows = statement.query_map(params![now, now, batch_size], row_to_check)?;
        rows.collect::<rusqlite::Result<Vec<_>>>()?
    };
    transaction.commit()?;
    Ok(claimed)
}

/// `reclaim_stale_checks_async`: release up to `batch_size` claims whose
/// lease of `lease_seconds` expired, oldest first.
///
/// The probe runs before the write transaction, as in Python (a `SELECT`
/// does not open an implicit transaction there), so an iteration without
/// stale claims takes no write lock.
pub(crate) fn reclaim_stale_checks(
    connection: &mut Connection,
    now: i64,
    lease_seconds: i64,
    batch_size: i64,
) -> Result<Vec<Check>, StoreError> {
    let stale_before = now.saturating_sub(lease_seconds);
    let candidate = connection
        .query_row(
            "SELECT 1 FROM health_check
             WHERE status = 'processing'
               AND (COALESCE(processing_started_at, 0) = 0
                    OR processing_started_at <= ?)
             LIMIT 1",
            [stale_before],
            |_| Ok(()),
        )
        .optional()?;
    if candidate.is_none() {
        return Ok(Vec::new());
    }
    let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
    // Legacy or malformed claims without a timestamp receive a full lease
    // from first observation instead of being reclaimed immediately.
    transaction.execute(
        "UPDATE health_check
            SET processing_started_at = ?
            WHERE status = 'processing'
              AND COALESCE(processing_started_at, 0) = 0",
        [now],
    )?;
    let reclaimed = {
        let mut statement = transaction.prepare(&format!(
            "UPDATE health_check
                SET status = 'idle', processing_started_at = 0
                WHERE id IN (
                    SELECT id FROM health_check
                    WHERE status = 'processing'
                      AND processing_started_at > 0
                      AND processing_started_at <= ?
                    ORDER BY processing_started_at ASC, id ASC
                    LIMIT ?
                )
                RETURNING {CHECK_COLUMNS}"
        ))?;
        let rows = statement.query_map(params![stale_before, batch_size], row_to_check)?;
        rows.collect::<rusqlite::Result<Vec<_>>>()?
    };
    transaction.commit()?;
    Ok(reclaimed)
}

/// `release_claims_async`: hand this process's own claims back on shutdown.
/// Only a claim still carrying the exact claim timestamp is released.
pub(crate) fn release_claims(
    connection: &mut Connection,
    now: i64,
    claims: &[(i64, i64)],
) -> Result<usize, StoreError> {
    if claims.is_empty() {
        return Ok(0);
    }
    let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let mut released = 0;
    {
        let mut statement = transaction.prepare(
            "UPDATE health_check
                SET status = 'idle', processing_started_at = 0,
                    next_check_time = ? + MAX(1, check_interval)
                WHERE id = ? AND status = 'processing'
                  AND processing_started_at = ? AND ? > 0",
        )?;
        for &(check_id, claim_started_at) in claims {
            released +=
                statement.execute(params![now, check_id, claim_started_at, claim_started_at])?;
        }
    }
    transaction.commit()?;
    Ok(released)
}

/// `_acknowledge_notification_attempt_async`.
fn acknowledge_notification_attempt(
    connection: &mut Connection,
    check_id: i64,
    attempt_seq: i64,
) -> Result<bool, StoreError> {
    let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let changed = transaction.execute(
        "UPDATE check_notification_state SET attempt_at = 0
            WHERE check_id = ? AND attempt_seq = ? AND attempt_at <> 0",
        [check_id, attempt_seq],
    )?;
    transaction.commit()?;
    Ok(changed > 0)
}

/// `list_held_checks_async`: every check with `held_since > 0`, by id.
fn list_held_checks(connection: &Connection) -> Result<Vec<HeldCheck>, StoreError> {
    let mut statement = connection.prepare(
        "SELECT hc.id, hc.data, hc.status, hc.disabled, hc.next_check_time,
                ns.held_since
            FROM check_notification_state AS ns
            JOIN health_check AS hc ON hc.id = ns.check_id
            WHERE ns.held_since > 0
            ORDER BY hc.id ASC",
    )?;
    let rows = statement.query_map([], |row| {
        Ok(HeldCheck {
            check_id: int_or_zero(row, 0)?,
            data: decode_check_data(row.get_ref(1)?),
            status: text_or_empty(row, 2)?,
            disabled: truthy(row, 3)?,
            next_check_time: int_or_zero(row, 4)?,
            held_since: int_or_zero(row, 5)?,
        })
    })?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

fn count(connection: &Connection, sql: &str, params: &[i64]) -> Result<i64, StoreError> {
    let value = connection.query_row(sql, params_from_iter(params), |row| int_or_zero(row, 0))?;
    Ok(value)
}

/// `reschedule_checks_async`: pull idle, enabled checks forward to `run_at`
/// (never delays a check that is already due earlier).
fn reschedule_checks(
    connection: &mut Connection,
    check_ids: &[i64],
    run_at: i64,
) -> Result<usize, StoreError> {
    if check_ids.is_empty() {
        return Ok(0);
    }
    let placeholders = vec!["?"; check_ids.len()].join(",");
    let sql = format!(
        "UPDATE health_check SET next_check_time = ?
            WHERE id IN ({placeholders})
              AND status = 'idle'
              AND disabled = 0
              AND next_check_time > ?"
    );
    let mut values = Vec::with_capacity(check_ids.len() + 2);
    values.push(run_at);
    values.extend_from_slice(check_ids);
    values.push(run_at);
    let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let changed = transaction.execute(&sql, params_from_iter(values))?;
    transaction.commit()?;
    Ok(changed)
}

impl Store {
    /// Every check (`list_async`; startup validation, lease estimation).
    pub async fn list_checks(&self) -> Result<Vec<Check>, StoreError> {
        self.call(|connection| list_checks(connection)).await
    }

    /// Claim up to `batch_size` due checks at `now` (`list_due_checks_async`;
    /// `batch_size` is `check_batch_size()`).
    pub async fn claim_due_checks(
        &self,
        now: i64,
        batch_size: i64,
    ) -> Result<Vec<Check>, StoreError> {
        self.call(move |connection| claim_due_checks(connection, now, batch_size))
            .await
    }

    /// Reclaim up to `batch_size` expired leases (`reclaim_stale_checks_async`).
    /// Returned checks have `processing_started_at = claim_started_at = 0`.
    pub async fn reclaim_stale_checks(
        &self,
        now: i64,
        lease_seconds: i64,
        batch_size: i64,
    ) -> Result<Vec<Check>, StoreError> {
        self.call(move |connection| {
            reclaim_stale_checks(connection, now, lease_seconds, batch_size)
        })
        .await
    }

    /// Release this process's claims on shutdown (`release_claims_async`);
    /// returns how many rows were released.
    pub async fn release_claims(&self, now: i64, claims: &[Check]) -> Result<usize, StoreError> {
        let claims: Vec<(i64, i64)> = claims
            .iter()
            .map(|check| (check.check_id, check.claim_started_at))
            .collect();
        self.call(move |connection| release_claims(connection, now, &claims))
            .await
    }

    /// The notification state of a check; a missing row reads as zeros.
    pub async fn get_notification_state(
        &self,
        check_id: i64,
    ) -> Result<NotificationState, StoreError> {
        self.call(move |connection| select_notification_state(connection, check_id))
            .await
    }

    /// Clear the pending delivery marker of attempt `attempt_seq`
    /// (`_acknowledge_notification_attempt_async`).
    pub async fn acknowledge_notification_attempt(
        &self,
        check_id: i64,
        attempt_seq: i64,
    ) -> Result<bool, StoreError> {
        self.call(move |connection| {
            acknowledge_notification_attempt(connection, check_id, attempt_seq)
        })
        .await
    }

    /// Checks held by a site dependency (`list_held_checks_async`).
    pub async fn list_held_checks(&self) -> Result<Vec<HeldCheck>, StoreError> {
        self.call(|connection| list_held_checks(connection)).await
    }

    /// `count_held_checks_async`.
    pub async fn count_held_checks(&self) -> Result<i64, StoreError> {
        self.call(|connection| {
            count(
                connection,
                "SELECT COUNT(*) FROM check_notification_state WHERE held_since > 0",
                &[],
            )
        })
        .await
    }

    /// Executions claimed before `epoch` that are still in flight
    /// (`count_processing_claims_before_async`).
    pub async fn count_processing_claims_before(&self, epoch: i64) -> Result<i64, StoreError> {
        self.call(move |connection| {
            count(
                connection,
                "SELECT COUNT(*) FROM health_check
                    WHERE status = 'processing'
                      AND processing_started_at > 0
                      AND processing_started_at < ?",
                &[epoch],
            )
        })
        .await
    }

    /// Pull idle, enabled checks forward to `run_at` (`reschedule_checks_async`).
    pub async fn reschedule_checks(
        &self,
        check_ids: &[i64],
        run_at: i64,
    ) -> Result<usize, StoreError> {
        let check_ids = check_ids.to_vec();
        self.call(move |connection| reschedule_checks(connection, &check_ids, run_at))
            .await
    }
}
