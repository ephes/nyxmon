//! Domain types (`domain/models.py`, `adapters/repositories/interface.py`).

use std::fmt;

use crate::pyjson::{PyDict, PyJson};

/// `ResultStatus` (`"ok"`, `"warning"`, `"error"`).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ResultStatus {
    Ok,
    Warning,
    Error,
}

impl ResultStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            ResultStatus::Ok => "ok",
            ResultStatus::Warning => "warning",
            ResultStatus::Error => "error",
        }
    }

    pub fn parse(value: &str) -> Option<ResultStatus> {
        match value {
            "ok" => Some(ResultStatus::Ok),
            "warning" => Some(ResultStatus::Warning),
            "error" => Some(ResultStatus::Error),
            _ => None,
        }
    }
}

impl fmt::Display for ResultStatus {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// `CheckStatus.IDLE`, the scheduling status of a check that is not claimed.
pub const CHECK_STATUS_IDLE: &str = "idle";
/// `CheckStatus.PROCESSING`, the scheduling status of a claimed check.
pub const CHECK_STATUS_PROCESSING: &str = "processing";

/// A `health_check` row (`domain/models.py:Check`).
#[derive(Clone, Debug, PartialEq)]
pub struct Check {
    pub check_id: i64,
    pub service_id: i64,
    pub name: String,
    pub check_type: String,
    pub url: String,
    pub check_interval: i64,
    pub next_check_time: i64,
    pub processing_started_at: i64,
    /// `idle` or `processing`; kept as text like the column.
    pub status: String,
    pub disabled: bool,
    /// The decoded `data` column, any JSON type (PLAN.md D-11).
    pub data: PyJson,
    /// The claim identity: `processing_started_at` at load time, kept after
    /// [`Check::schedule_next_check`] so a late completion cannot clobber a
    /// newer claim.
    pub claim_started_at: i64,
}

impl Check {
    /// `Check.schedule_next_check()` with `now = int(time.time())`.
    pub fn schedule_next_check(&mut self, now: i64) {
        self.next_check_time = now.saturating_add(self.check_interval);
        self.status = CHECK_STATUS_IDLE.to_owned();
        self.processing_started_at = 0;
    }
}

/// The result of one check execution (`domain/models.py:Result`).
#[derive(Clone, Debug, PartialEq)]
pub struct CheckResult {
    pub check_id: i64,
    pub status: ResultStatus,
    /// Key order is user-visible (PLAN.md section 1.3).
    pub data: PyDict,
}

impl CheckResult {
    pub fn new(check_id: i64, status: ResultStatus, data: PyDict) -> Self {
        CheckResult {
            check_id,
            status,
            data,
        }
    }
}

/// Per-check alert cadence state (`interface.py:NotificationState`).
///
/// Equality is field-wise; it drives the compare-and-swap of the result
/// persistence.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub struct NotificationState {
    pub failure_count: i64,
    pub last_attempt_count: i64,
    pub last_immediate_at: i64,
    pub last_notified_at: i64,
    pub first_failure_at: i64,
    pub held_since: i64,
    pub attempt_seq: i64,
    pub attempt_at: i64,
}

/// `NOTIFICATION_STATE_COLUMNS`, in row order.
pub const NOTIFICATION_STATE_COLUMNS: [&str; 8] = [
    "failure_count",
    "last_attempt_count",
    "last_immediate_at",
    "last_notified_at",
    "first_failure_at",
    "held_since",
    "attempt_seq",
    "attempt_at",
];

impl NotificationState {
    /// `NotificationState.from_row`: missing values (pre-upgrade rows or
    /// NULLs) read as 0.
    pub fn from_row(row: Option<&[Option<i64>]>) -> Self {
        let Some(row) = row else {
            return NotificationState::default();
        };
        let value = |index: usize| row.get(index).copied().flatten().unwrap_or(0);
        NotificationState {
            failure_count: value(0),
            last_attempt_count: value(1),
            last_immediate_at: value(2),
            last_notified_at: value(3),
            first_failure_at: value(4),
            held_since: value(5),
            attempt_seq: value(6),
            attempt_at: value(7),
        }
    }

    /// The values in [`NOTIFICATION_STATE_COLUMNS`] order.
    pub fn as_row(&self) -> [i64; 8] {
        [
            self.failure_count,
            self.last_attempt_count,
            self.last_immediate_at,
            self.last_notified_at,
            self.first_failure_at,
            self.held_since,
            self.attempt_seq,
            self.attempt_at,
        ]
    }

    /// Full reset when a check recovers; `attempt_seq` is carried over.
    pub fn cleared(&self) -> Self {
        NotificationState {
            attempt_seq: self.attempt_seq,
            ..NotificationState::default()
        }
    }

    /// Reset of the failure streak of a maintenance-suppressed sample; keeps
    /// `last_immediate_at`, `held_since`, `attempt_seq` and `attempt_at`.
    pub fn with_streak_reset(&self) -> Self {
        NotificationState {
            last_immediate_at: self.last_immediate_at,
            held_since: self.held_since,
            attempt_seq: self.attempt_seq,
            attempt_at: self.attempt_at,
            ..NotificationState::default()
        }
    }
}

/// `handlers.py:_NotificationDecision`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NotificationDecision {
    pub should_notify: bool,
    /// The state the transition was calculated from.
    pub expected: NotificationState,
    /// The state to swap in.
    pub next_state: NotificationState,
    /// Whether a site dependency deferred the notification.
    pub held: bool,
    /// The attempt sequence stamped by this transition, 0 when none.
    pub attempt_seq: i64,
    /// The epoch the decision was taken at.
    pub now: i64,
}

/// A persisted collector/batch-level incident (`interface.py:CollectorIncident`).
#[derive(Clone, Debug, PartialEq)]
pub struct CollectorIncident {
    pub incident_key: String,
    pub opened_at: i64,
    pub last_alert_at: i64,
    pub alert_count: i64,
    /// Decoded payload; undecodable or non-object payloads read as `{}`.
    pub payload: PyDict,
}

/// Outcome of claiming an incident alert (`interface.py:CollectorIncidentAlert`).
#[derive(Clone, Debug, PartialEq)]
pub struct CollectorIncidentAlert {
    pub incident: CollectorIncident,
    pub should_notify: bool,
    pub is_new: bool,
}

/// A check whose alerting is held by a site dependency (`interface.py:HeldCheck`).
#[derive(Clone, Debug, PartialEq)]
pub struct HeldCheck {
    pub check_id: i64,
    /// The check's `data` column (carries `site_dependency`).
    pub data: PyJson,
    pub status: String,
    pub disabled: bool,
    pub next_check_time: i64,
    pub held_since: i64,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_state() -> NotificationState {
        NotificationState {
            failure_count: 3,
            last_attempt_count: 2,
            last_immediate_at: 10,
            last_notified_at: 20,
            first_failure_at: 5,
            held_since: 7,
            attempt_seq: 4,
            attempt_at: 30,
        }
    }

    #[test]
    fn cleared_keeps_only_attempt_seq() {
        let state = sample_state().cleared();
        assert_eq!(
            state,
            NotificationState {
                attempt_seq: 4,
                ..NotificationState::default()
            }
        );
    }

    #[test]
    fn streak_reset_keeps_hold_and_delivery_fields() {
        let state = sample_state().with_streak_reset();
        assert_eq!(state.as_row(), [0, 0, 10, 0, 0, 7, 4, 30]);
    }

    #[test]
    fn from_row_tolerates_short_rows_and_nulls() {
        assert_eq!(
            NotificationState::from_row(None),
            NotificationState::default()
        );
        let row = [Some(1), None, Some(3)];
        assert_eq!(
            NotificationState::from_row(Some(&row)).as_row(),
            [1, 0, 3, 0, 0, 0, 0, 0]
        );
    }

    #[test]
    fn schedule_next_check_keeps_claim_identity() {
        let mut check = Check {
            check_id: 1,
            service_id: 2,
            name: String::new(),
            check_type: "http".to_owned(),
            url: "https://example.invalid".to_owned(),
            check_interval: 300,
            next_check_time: 0,
            processing_started_at: 1000,
            status: CHECK_STATUS_PROCESSING.to_owned(),
            disabled: false,
            data: PyJson::dict(),
            claim_started_at: 1000,
        };
        check.schedule_next_check(2000);
        assert_eq!(check.next_check_time, 2300);
        assert_eq!(check.status, CHECK_STATUS_IDLE);
        assert_eq!(check.processing_started_at, 0);
        assert_eq!(check.claim_started_at, 1000);
    }

    #[test]
    fn result_status_round_trips() {
        for status in [ResultStatus::Ok, ResultStatus::Warning, ResultStatus::Error] {
            assert_eq!(ResultStatus::parse(status.as_str()), Some(status));
        }
        assert_eq!(ResultStatus::parse("passed"), None);
    }
}
