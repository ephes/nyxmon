//! Result retention (`SqliteResultRepository.delete_old_results_async`;
//! PLAN.md section 3.4 "Cleaner batch").

use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params, params_from_iter};

use crate::actor::Store;
use crate::error::StoreError;

/// Maximum rows pruned from each table in one transaction.
/// Bounds allocation and SQLite's parameter count for the result-id list.
pub const MAX_CLEANUP_BATCH_SIZE: i64 = 1000;

/// Counts from one bounded cleanup batch. Session deletions do not contribute
/// to reported result totals; a full batch in either table calls for another pass.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct CleanupBatch {
    pub results_deleted: usize,
    pub sessions_deleted: usize,
}

/// Delete up to `batch_size` expired sessions, then up to `batch_size`
/// expired results, in separate transactions. Validate both arguments before
/// any deletion, including when the sessions table is absent.
pub(crate) fn delete_old_results(
    connection: &mut Connection,
    retention_seconds: i64,
    batch_size: i64,
) -> Result<CleanupBatch, StoreError> {
    if retention_seconds <= 0 {
        return Err(StoreError::InvalidArgument(
            "retention_seconds must be strictly positive",
        ));
    }
    if !(1..=MAX_CLEANUP_BATCH_SIZE).contains(&batch_size) {
        return Err(StoreError::InvalidArgument(
            "batch_size must be in 1..=1000",
        ));
    }
    let mut batch = CleanupBatch::default();
    // The agent also supports databases without the sessions table; the
    // probe runs every batch, like Python's.
    let sessions = connection
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'django_session'",
            [],
            |_| Ok(()),
        )
        .optional()?;
    if sessions.is_some() {
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        batch.sessions_deleted = transaction.execute(
            "DELETE FROM django_session WHERE session_key IN \
             (SELECT session_key FROM django_session \
             WHERE expire_date < datetime('now') ORDER BY expire_date LIMIT ?)",
            [batch_size],
        )?;
        transaction.commit()?;
    }

    // `created_at` is SQLite's UTC `datetime('now')`, so the cutoff is
    // computed by SQLite on the same clock.
    let modifier = format!("-{retention_seconds} seconds");
    let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let ids: Vec<i64> = {
        let mut statement = transaction.prepare(
            "SELECT id FROM check_result \
             WHERE created_at < datetime('now', ?) ORDER BY id LIMIT ?",
        )?;
        let rows = statement.query_map(params![modifier, batch_size], |row| row.get(0))?;
        rows.collect::<rusqlite::Result<_>>()?
    };
    if ids.is_empty() {
        transaction.commit()?;
        return Ok(batch);
    }
    let placeholders = vec!["?"; ids.len()].join(",");
    let deleted = transaction.execute(
        &format!("DELETE FROM check_result WHERE id IN ({placeholders})"),
        params_from_iter(&ids),
    )?;
    transaction.commit()?;
    batch.results_deleted = deleted;
    Ok(batch)
}

impl Store {
    /// One cleaner batch (`delete_old_results_async`). `batch_size` must be
    /// in `1..=MAX_CLEANUP_BATCH_SIZE`, retention strictly positive. Returns
    /// separate result and session counts; either full count warrants a retry.
    pub async fn delete_old_results(
        &self,
        retention_seconds: i64,
        batch_size: i64,
    ) -> Result<CleanupBatch, StoreError> {
        self.call(move |connection| delete_old_results(connection, retention_seconds, batch_size))
            .await
    }
}
