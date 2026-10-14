//! Result persistence (`SqliteStore._persist_check_result_async`; PLAN.md
//! section 3.4 "Persist").

use nyxmon_core::PyJson;
use nyxmon_core::model::{Check, CheckResult, NOTIFICATION_STATE_COLUMNS, NotificationState};
use rusqlite::{Connection, OptionalExtension, Transaction, TransactionBehavior, params};

use crate::LOGGER;
use crate::actor::Store;
use crate::error::StoreError;
use crate::rows::select_notification_state;

/// One call of `persist_check_result`.
#[derive(Clone, Debug)]
pub struct PersistRequest {
    /// The check after `schedule_next_check` (its `status`,
    /// `next_check_time`, `processing_started_at` are written; its
    /// `claim_started_at` guards the completion).
    pub check: Check,
    pub result: CheckResult,
    /// `(expected, next)` notification states for the compare-and-swap.
    pub transition: Option<(NotificationState, NotificationState)>,
    /// Whether to complete the claim (`complete_check`, default `true`).
    pub complete_check: bool,
    /// `held_since` to stamp when no transition is given (the
    /// conflict-exhaustion fallback).
    pub hold_marker: Option<i64>,
}

impl PersistRequest {
    /// A completing persist without transition or hold marker.
    pub fn new(check: Check, result: CheckResult) -> Self {
        PersistRequest {
            check,
            result,
            transition: None,
            complete_check: true,
            hold_marker: None,
        }
    }
}

/// `_complete_on_connection`: complete the claim unless a newer one exists.
fn complete(transaction: &Transaction<'_>, check: &Check) -> Result<bool, StoreError> {
    let changed = transaction.execute(
        "UPDATE health_check
            SET status = ?, next_check_time = ?, processing_started_at = ?
            WHERE id = ?
              AND ((status = 'processing' AND processing_started_at = ?)
                   OR (status <> 'processing' AND ? = 0))",
        params![
            check.status,
            check.next_check_time,
            check.processing_started_at,
            check.check_id,
            check.claim_started_at,
            check.claim_started_at,
        ],
    )?;
    Ok(changed > 0)
}

/// `_insert_on_connection`: `created_at` is SQLite's UTC `datetime('now')`.
fn insert_result(transaction: &Transaction<'_>, result: CheckResult) -> Result<(), StoreError> {
    let CheckResult {
        check_id,
        status,
        data,
    } = result;
    let data = PyJson::Dict(data)
        .to_json_string()
        .map_err(|err| StoreError::Json(err.to_string()))?;
    transaction.execute(
        "INSERT INTO check_result (health_check_id, status, data, created_at)
            VALUES (?, ?, ?, datetime('now'))",
        params![check_id, status.as_str(), data],
    )?;
    Ok(())
}

fn state_columns() -> String {
    NOTIFICATION_STATE_COLUMNS.join(", ")
}

/// `_upsert_notification_state`: write all eight columns.
fn upsert_notification_state(
    transaction: &Transaction<'_>,
    check_id: i64,
    state: &NotificationState,
) -> Result<(), StoreError> {
    let assignments = NOTIFICATION_STATE_COLUMNS
        .iter()
        .map(|column| format!("{column} = excluded.{column}"))
        .collect::<Vec<_>>()
        .join(", ");
    let sql = format!(
        "INSERT INTO check_notification_state (check_id, {}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(check_id) DO UPDATE SET {assignments}",
        state_columns()
    );
    let [a, b, c, d, e, f, g, h] = state.as_row();
    transaction.execute(&sql, params![check_id, a, b, c, d, e, f, g, h])?;
    Ok(())
}

/// `_mark_held_since`: stamp `held_since` unless a hold is already running.
fn mark_held_since(
    transaction: &Transaction<'_>,
    check_id: i64,
    hold_marker: i64,
) -> Result<(), StoreError> {
    let changed = transaction.execute(
        "UPDATE check_notification_state SET held_since = ?
            WHERE check_id = ? AND held_since = 0",
        [hold_marker, check_id],
    )?;
    if changed > 0
        || check_exists_in(
            transaction,
            "check_notification_state",
            "check_id",
            check_id,
        )?
    {
        // Stamped, or a hold is already running and its start bounds it.
        return Ok(());
    }
    let state = NotificationState {
        held_since: hold_marker,
        ..NotificationState::default()
    };
    let [a, b, c, d, e, f, g, h] = state.as_row();
    transaction.execute(
        &format!(
            "INSERT INTO check_notification_state (check_id, {}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            state_columns()
        ),
        params![check_id, a, b, c, d, e, f, g, h],
    )?;
    Ok(())
}

fn check_exists_in(
    connection: &Connection,
    table: &str,
    column: &str,
    id: i64,
) -> Result<bool, StoreError> {
    let sql = format!("SELECT 1 FROM {table} WHERE {column} = ?");
    Ok(connection
        .query_row(&sql, [id], |_| Ok(()))
        .optional()?
        .is_some())
}

/// `_persist_check_result_async`, in one `BEGIN IMMEDIATE` transaction.
///
/// Returns `false` when the result was dropped for a deleted check or stored
/// without completing because a newer claim exists (the notification state
/// is then not touched and nothing may be sent).
pub(crate) fn persist_check_result(
    connection: &mut Connection,
    request: PersistRequest,
) -> Result<bool, StoreError> {
    let PersistRequest {
        check,
        result,
        transition,
        complete_check,
        hold_marker,
    } = request;
    let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let completion_applied = if complete_check {
        complete(&transaction, &check)?
    } else {
        true
    };
    if !complete_check || !completion_applied {
        if !check_exists_in(&transaction, "health_check", "id", check.check_id)? {
            transaction.rollback()?;
            tracing::info!(
                target: LOGGER,
                "dropped late result for deleted check_id={}",
                check.check_id
            );
            return Ok(false);
        }
        if complete_check {
            tracing::info!(
                target: LOGGER,
                "stored late result without changing newer claim for check_id={}",
                check.check_id
            );
        }
    }
    insert_result(&transaction, result)?;
    if complete_check && !completion_applied {
        transaction.commit()?;
        return Ok(false);
    }
    if let Some((expected, next)) = transition {
        let current = select_notification_state(&transaction, check.check_id)?;
        if current != expected {
            transaction.rollback()?;
            return Err(StoreError::NotificationStateConflict {
                check_id: check.check_id,
            });
        }
        if next != expected {
            upsert_notification_state(&transaction, check.check_id, &next)?;
        }
    } else if let Some(hold_marker) = hold_marker {
        // The hold must land with the completion, or the observer could see
        // an idle, unheld row for a claim that owes a recheck.
        mark_held_since(&transaction, check.check_id, hold_marker)?;
    }
    transaction.commit()?;
    Ok(true)
}

impl Store {
    /// Persist one check result atomically with the claim completion and
    /// the notification-state transition (`persist_check_result`).
    ///
    /// # Errors
    /// [`StoreError::NotificationStateConflict`] when the stored state no
    /// longer equals the transition's expectation (everything rolled back);
    /// SQLite errors (constraint violations such as a `NaN` in the data
    /// failing `JSON_VALID`, busy timeout) also roll everything back.
    pub async fn persist_check_result(&self, request: PersistRequest) -> Result<bool, StoreError> {
        self.call(move |connection| persist_check_result(connection, request))
            .await
    }
}
