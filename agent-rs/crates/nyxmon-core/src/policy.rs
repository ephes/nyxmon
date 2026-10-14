//! Per-check notification policy (`service_layer/notification_policy.py`).
//!
//! The global defaults come from the environment readers in [`crate::env`];
//! per-check overrides live in `health_check.data["notification_policy"]`.
//! Malformed overrides warn once per `(check_id, key)` and fall back to the
//! global default, never raising (PLAN.md section 5.2).

use num_bigint::BigInt;

use crate::env::{
    Env, MAX_CONSECUTIVE_FAILURES, MAX_REMINDER_SECONDS, MIN_REMINDER_SECONDS,
    global_consecutive_failures, global_reminder_seconds,
};
use crate::log::{WarnOnce, loggers};
use crate::model::ResultStatus;
use crate::pyjson::{PyDict, PyJson, bigint_to_i64_saturating, py_repr};

/// `POLICY_DATA_KEY`.
pub const POLICY_DATA_KEY: &str = "notification_policy";

/// [`WarnOnce`] scope of `_warned_policy_fields`.
pub const WARN_SCOPE: &str = "notification_policy";

/// `NotificationPolicy`: the resolved policy of one check and severity.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NotificationPolicy {
    pub consecutive_failures: i64,
    pub reminder_seconds: i64,
}

/// `_warn_once`.
fn warn_once(warnings: &WarnOnce, check_id: i64, key: &str, raw: &PyJson, reason: &str) {
    warnings.warn(
        loggers::NOTIFICATION_POLICY,
        WARN_SCOPE,
        check_id,
        key,
        || {
            format!(
                "notification_policy.{key} for check_id={check_id} is invalid ({reason}, got {}); \
                 using the global default",
                py_repr(raw)
            )
        },
    );
}

/// `_bounded_int`: `None` when the key is absent or invalid (with a warning).
fn bounded_int(
    warnings: &WarnOnce,
    config: &PyDict,
    key: &str,
    check_id: i64,
    low: i64,
    high: i64,
) -> Option<i64> {
    let raw = config.get(key)?;
    // `isinstance(raw, bool) or not isinstance(raw, int)`: floats such as
    // `2.0` are rejected too.
    let value = match raw {
        PyJson::Int(value) => BigInt::from(*value),
        PyJson::BigInt(value) => value.clone(),
        _ => {
            warn_once(warnings, check_id, key, raw, "must be an integer");
            return None;
        }
    };
    if value < BigInt::from(low) || value > BigInt::from(high) {
        warn_once(
            warnings,
            check_id,
            key,
            raw,
            &format!("must be between {low} and {high}"),
        );
        return None;
    }
    Some(bigint_to_i64_saturating(&value))
}

/// `resolve_notification_policy(check, status)`.
///
/// `data` is the check's raw `data` column; a non-object resolves to the
/// global defaults (PLAN.md D-11). Never fails.
pub fn resolve_notification_policy(
    env: &Env,
    warnings: &WarnOnce,
    check_id: i64,
    data: &PyJson,
    status: ResultStatus,
) -> NotificationPolicy {
    let default_threshold = global_consecutive_failures(env);
    let default_reminder = global_reminder_seconds(env, status);
    let defaults = NotificationPolicy {
        consecutive_failures: default_threshold,
        reminder_seconds: default_reminder,
    };

    let Some(config) = data.get(POLICY_DATA_KEY) else {
        return defaults;
    };
    let config = match config {
        PyJson::Null => return defaults,
        PyJson::Dict(config) => config,
        other => {
            warn_once(
                warnings,
                check_id,
                POLICY_DATA_KEY,
                other,
                "must be an object",
            );
            return defaults;
        }
    };

    // Python evaluates every argument of `_first_not_none` (and so warns
    // about each malformed key) before picking the first usable value.
    let is_warning = status == ResultStatus::Warning;
    let warning_threshold = if is_warning {
        bounded_int(
            warnings,
            config,
            "warning_consecutive_failures",
            check_id,
            1,
            MAX_CONSECUTIVE_FAILURES,
        )
    } else {
        None
    };
    let threshold = bounded_int(
        warnings,
        config,
        "consecutive_failures",
        check_id,
        1,
        MAX_CONSECUTIVE_FAILURES,
    );
    let warning_reminder = if is_warning {
        bounded_int(
            warnings,
            config,
            "warning_reminder_seconds",
            check_id,
            MIN_REMINDER_SECONDS,
            MAX_REMINDER_SECONDS,
        )
    } else {
        None
    };
    let reminder = bounded_int(
        warnings,
        config,
        "reminder_seconds",
        check_id,
        MIN_REMINDER_SECONDS,
        MAX_REMINDER_SECONDS,
    );
    NotificationPolicy {
        consecutive_failures: warning_threshold.or(threshold).unwrap_or(default_threshold),
        reminder_seconds: warning_reminder.or(reminder).unwrap_or(default_reminder),
    }
}
