//! Result retention (`SqliteResultRepository.delete_old_results_async`;
//! PLAN.md section 3.4 "Cleaner batch").

use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params, params_from_iter};

use crate::actor::Store;
use crate::error::StoreError;

/// One cleaner batch: delete up to `batch_size` expired Django sessions (if
/// the table exists) and commit, then up to `batch_size` results older than
/// `retention_seconds` (by id) and commit. Returns the number of results
/// deleted; fewer than `batch_size` means none are left.
pub(crate) fn delete_old_results(
    connection: &mut Connection,
    retention_seconds: i64,
    batch_size: i64,
) -> Result<usize, StoreError> {
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
        transaction.execute(
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
        return Ok(0);
    }
    let placeholders = vec!["?"; ids.len()].join(",");
    let deleted = transaction.execute(
        &format!("DELETE FROM check_result WHERE id IN ({placeholders})"),
        params_from_iter(&ids),
    )?;
    transaction.commit()?;
    Ok(deleted)
}

impl Store {
    /// One cleaner batch (`delete_old_results_async`).
    pub async fn delete_old_results(
        &self,
        retention_seconds: i64,
        batch_size: i64,
    ) -> Result<usize, StoreError> {
        self.call(move |connection| delete_old_results(connection, retention_seconds, batch_size))
            .await
    }
}
