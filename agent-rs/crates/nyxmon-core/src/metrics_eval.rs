//! The pure evaluation of the json-metrics check
//! (`json_metrics_executor.py`: `_evaluate`, `_resolve_path`,
//! `_bounded_actual`, `_build_failure_summary`), pinned by
//! `fixtures/metrics_eval.json`.

use crate::config::PyException;
use crate::config::json_metrics::{MetricCheck, Severity};
use crate::model::ResultStatus;
use crate::pyjson::{PyDict, PyJson, actual_prefix, char_len, py_cmp};

/// `MAX_ACTUAL_CHARS`.
pub const MAX_ACTUAL_CHARS: usize = 200;

/// Shared serialization allowance per evaluation (truncation markers excluded).
pub const MAX_DIAGNOSTIC_CHARS: usize = 16_384;

/// CPython's default limit for `int(<str>)` (`sys.int_info.default_max_str_digits`).
const MAX_INT_STR_DIGITS: usize = 4300;

/// `re.sub(r"\[(\d+)\]", r".\1", path)` with ASCII digits (PLAN.md D-6).
fn normalise_brackets(path: &str) -> String {
    let mut out = String::with_capacity(path.len());
    let mut rest = path;
    while let Some(open) = rest.find('[') {
        out.push_str(&rest[..open]);
        let after = &rest[open + 1..];
        let digits = after.bytes().take_while(u8::is_ascii_digit).count();
        if digits > 0 && after.as_bytes().get(digits) == Some(&b']') {
            out.push('.');
            out.push_str(&after[..digits]);
            rest = &after[digits + 1..];
        } else {
            out.push('[');
            rest = after;
        }
    }
    out.push_str(rest);
    out
}

/// `_resolve_path(payload, path)`: `Ok(None)` is `_MISSING`.
///
/// # Errors
/// `ValueError` when a list index has more digits than `int()` converts.
pub fn resolve_path<'a>(
    payload: &'a PyJson,
    path: &str,
) -> Result<Option<&'a PyJson>, PyException> {
    if path == "$" {
        return Ok(Some(payload));
    }
    let normalised = normalise_brackets(path);
    let rooted = normalised.strip_prefix("$.").unwrap_or(&normalised);
    let mut current = payload;
    for part in rooted.split('.').filter(|part| !part.is_empty()) {
        if let Some(value) = current.as_dict().and_then(|map| map.get(part)) {
            current = value;
            continue;
        }
        let Some(items) = current.as_list() else {
            return Ok(None);
        };
        if !part.bytes().all(|b| b.is_ascii_digit()) {
            return Ok(None);
        }
        let significant = part.trim_start_matches('0');
        if part.len() > MAX_INT_STR_DIGITS {
            return Err(PyException::VALUE_ERROR);
        }
        match significant.parse::<usize>() {
            Ok(index) if index < items.len() => current = &items[index],
            _ if significant.is_empty() && !items.is_empty() => current = &items[0],
            _ => return Ok(None),
        }
    }
    Ok(Some(current))
}

/// `_bounded_actual(actual)`: the `actual` value of a failure and, when its
/// JSON form is longer than [`MAX_ACTUAL_CHARS`], the truncation marker.
pub fn bounded_actual(actual: &PyJson) -> (PyJson, bool) {
    let mut remaining = MAX_DIAGNOSTIC_CHARS;
    bounded_actual_with_budget(actual, &mut remaining)
}

fn bounded_actual_with_budget(actual: &PyJson, remaining: &mut usize) -> (PyJson, bool) {
    if *remaining == 0 {
        return (PyJson::from("…"), true);
    }
    // Reserve one character of work for detecting overflow.
    let limit = MAX_ACTUAL_CHARS.min(remaining.saturating_sub(1));
    let (mut rendered, truncated) = actual_prefix(actual, limit);
    // Charge the lookahead character that established truncation as well.
    *remaining = remaining.saturating_sub(char_len(&rendered) + usize::from(truncated));
    if !truncated {
        return (actual.clone(), false);
    }
    rendered.push('\u{2026}');
    (PyJson::Str(rendered), true)
}

/// `_evaluate(payload, config)`: the failure records in check order.
///
/// # Errors
/// `TypeError` for a check whose `path` is not a string (`re.sub` on a
/// non-string), raised at the first such check as in Python.
pub fn evaluate(payload: &PyJson, checks: &[MetricCheck]) -> Result<Vec<PyJson>, PyException> {
    let mut failures = Vec::new();
    let mut diagnostic_budget = MAX_DIAGNOSTIC_CHARS;
    for check in checks {
        let PyJson::Str(path) = &check.path else {
            return Err(PyException::TYPE_ERROR);
        };
        let actual = resolve_path(payload, path)?;
        if actual.is_some_and(|actual| py_cmp(check.op, actual, &check.value).unwrap_or(false)) {
            continue;
        }
        let mut failure = PyDict::new();
        let limit = MAX_ACTUAL_CHARS.min(diagnostic_budget);
        let path_prefix: String = path.chars().take(limit).collect();
        diagnostic_budget = diagnostic_budget.saturating_sub(char_len(&path_prefix));
        let path_truncated = path_prefix.len() < path.len();
        failure.insert(
            "path".to_owned(),
            PyJson::from(if path_truncated {
                format!("{path_prefix}…")
            } else {
                path_prefix
            }),
        );
        failure.insert("op".to_owned(), PyJson::from(check.op.symbol()));
        let (expected, expected_truncated) =
            bounded_actual_with_budget(&check.value, &mut diagnostic_budget);
        failure.insert("expected".to_owned(), expected);
        let (bounded, truncated) = actual.map_or((PyJson::Null, false), |actual| {
            bounded_actual_with_budget(actual, &mut diagnostic_budget)
        });
        failure.insert("actual".to_owned(), bounded);
        if actual.is_none() {
            failure.insert("reason".to_owned(), PyJson::from("path_missing"));
        }
        failure.insert("severity".to_owned(), PyJson::from(check.severity.as_str()));
        if truncated {
            failure.insert("actual_truncated".to_owned(), PyJson::Bool(true));
        }
        if path_truncated {
            failure.insert("path_truncated".to_owned(), PyJson::Bool(true));
        }
        if expected_truncated {
            failure.insert("expected_truncated".to_owned(), PyJson::Bool(true));
        }
        failures.push(PyJson::Dict(failure));
    }
    Ok(failures)
}

fn severity_of(failure: &PyJson) -> Option<&str> {
    failure.get("severity").and_then(PyJson::as_str)
}

/// `_build_failure_summary(failures)`.
pub fn failure_summary(failures: &[PyJson]) -> String {
    const LIMIT: usize = 4096;
    const OMITTED: &str = " … [paths omitted]";
    let mut summary = String::new();
    // Reserve both counts before appending any path evidence.
    for severity in [Severity::Critical, Severity::Warning] {
        let count = failures
            .iter()
            .filter(|f| severity_of(f) == Some(severity.as_str()))
            .count();
        if count > 0 {
            if !summary.is_empty() {
                summary.push_str("; ");
            }
            summary.push_str(&format!("{count} {}", severity.as_str()));
        }
    }
    let mut parts = Vec::new();
    for severity in [Severity::Critical, Severity::Warning] {
        let count = failures
            .iter()
            .filter(|f| severity_of(f) == Some(severity.as_str()))
            .count();
        if count == 0 {
            continue;
        }
        let mut part = format!("{count} {}: ", severity.as_str());
        for (index, failure) in failures
            .iter()
            .filter(|f| severity_of(f) == Some(severity.as_str()))
            .enumerate()
        {
            if index > 0 {
                part.push_str(", ");
            }
            let path = failure.get("path").and_then(PyJson::as_str).unwrap_or("?");
            if part.len() + path.len() > LIMIT - OMITTED.len() - summary.len() {
                summary.push_str(OMITTED);
                return summary;
            }
            part.push_str(path);
        }
        parts.push(part);
    }
    let full = parts.join("; ");
    if full.len() > LIMIT {
        summary.push_str(OMITTED);
        summary
    } else {
        full
    }
}

/// The result status of a non-empty failure list: `error` when any failure
/// is critical, else `warning`.
pub fn failure_status(failures: &[PyJson]) -> ResultStatus {
    if failures.iter().any(|f| severity_of(f) == Some("critical")) {
        ResultStatus::Error
    } else {
        ResultStatus::Warning
    }
}

#[cfg(test)]
mod tests;
