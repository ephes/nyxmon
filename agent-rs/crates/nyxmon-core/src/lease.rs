//! Runtime estimate of a configured check
//! (`adapters/collector.py:estimated_check_runtime_seconds`), PLAN.md
//! section 5.8 step 2.
//!
//! The collector extends the processing lease to the largest estimate of
//! its enabled checks. The estimator uses its own defaults (`timeout=10`,
//! `retries=0`, `retry_delay=0`) regardless of the executors' defaults.

use crate::log::{WarnOnce, loggers};
use crate::pyjson::{PyJson, py_float, py_float_of_int};

/// `MAX_DERIVED_PROCESSING_LEASE_SECONDS`.
pub const MAX_DERIVED_PROCESSING_LEASE_SECONDS: i64 = 3600;

/// [`WarnOnce`] scope of the collector's `_runtime_estimate_warning_keys`.
pub const WARN_SCOPE: &str = "runtime_estimate";

/// `min(max(0.0, parsed), MAX + 1)`.
fn clamp(parsed: f64) -> f64 {
    let low = if parsed > 0.0 { parsed } else { 0.0 };
    let high = (MAX_DERIVED_PROCESSING_LEASE_SECONDS + 1) as f64;
    if high < low { high } else { low }
}

/// `float(value)` when it is finite, for the value types the estimator
/// accepts (non-bool numbers and strings).
fn finite_number(value: &PyJson) -> Option<f64> {
    let parsed = match value {
        PyJson::Int(number) => *number as f64,
        PyJson::BigInt(number) => py_float_of_int(number).ok()?,
        PyJson::Float(number) => *number,
        PyJson::Str(text) => py_float(text).ok()?,
        _ => return None,
    };
    parsed.is_finite().then_some(parsed)
}

/// `estimated_check_runtime_seconds(check, warning_keys)`.
///
/// `warnings` is the collector's warn-once set; `None` (Python's
/// `warning_keys=None`) suppresses the warnings.
pub fn estimated_check_runtime_seconds(
    check_id: i64,
    check_type: &str,
    data: &PyJson,
    warnings: Option<&WarnOnce>,
) -> i64 {
    let non_negative_number = |name: &str, default: i64| -> f64 {
        let Some(value) = data.get(name) else {
            return default as f64;
        };
        if let Some(parsed) = finite_number(value) {
            return clamp(parsed);
        }
        if let Some(warnings) = warnings {
            warnings.warn(loggers::COLLECTOR, WARN_SCOPE, check_id, name, || {
                format!(
                    "check_id={check_id} has non-numeric {name}; using runtime-estimate default \
                     {default}"
                )
            });
        }
        default as f64
    };

    let explicit_budget = non_negative_number("max_runtime_seconds", 0);
    if explicit_budget != 0.0 {
        return explicit_budget.ceil() as i64;
    }

    if check_type == "ping" {
        // One single-echo process per attempt, each bounded by timeout plus
        // the process grace, preceded by a DNS lookup bounded by timeout.
        let count = non_negative_number("count", 3).floor().max(1.0);
        let timeout = non_negative_number("timeout", 5);
        let interval = non_negative_number("interval", 1);
        return ((count + 1.0) * (timeout + 2.0) + (count - 1.0) * interval + 30.0).ceil() as i64;
    }

    let retries = non_negative_number("retries", 0).floor();
    let retry_delay = non_negative_number("retry_delay", 0);
    let timeout = non_negative_number("timeout", 10);
    let per_attempt = match check_type {
        "imap" => timeout * 8.0,
        "smtp" => timeout * 5.0,
        "tcp" => {
            non_negative_number("connect_timeout", 10)
                + non_negative_number("tls_handshake_timeout", 10)
                + timeout
        }
        _ => timeout,
    };
    ((retries + 1.0) * per_attempt + retries * retry_delay + 30.0).ceil() as i64
}
