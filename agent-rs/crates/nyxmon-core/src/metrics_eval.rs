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
        let mut failure = PyDict::new();
        failure.insert("path".to_owned(), check.path.clone());
        failure.insert("op".to_owned(), PyJson::from(check.op.symbol()));
        failure.insert("expected".to_owned(), check.value.clone());
        let Some(actual) = resolve_path(payload, path)? else {
            failure.insert("actual".to_owned(), PyJson::Null);
            failure.insert("reason".to_owned(), PyJson::from("path_missing"));
            failure.insert("severity".to_owned(), PyJson::from(check.severity.as_str()));
            failures.push(PyJson::Dict(failure));
            continue;
        };
        if py_cmp(check.op, actual, &check.value).unwrap_or(false) {
            continue;
        }
        let (bounded, truncated) = bounded_actual_with_budget(actual, &mut diagnostic_budget);
        failure.insert("actual".to_owned(), bounded);
        failure.insert("severity".to_owned(), PyJson::from(check.severity.as_str()));
        if truncated {
            failure.insert("actual_truncated".to_owned(), PyJson::Bool(true));
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
    let mut parts = Vec::new();
    for severity in [Severity::Critical, Severity::Warning] {
        let paths: Vec<&str> = failures
            .iter()
            .filter(|f| severity_of(f) == Some(severity.as_str()))
            .map(|f| f.get("path").and_then(PyJson::as_str).unwrap_or("?"))
            .collect();
        if !paths.is_empty() {
            parts.push(format!(
                "{} {}: {}",
                paths.len(),
                severity.as_str(),
                paths.join(", ")
            ));
        }
    }
    parts.join("; ")
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
