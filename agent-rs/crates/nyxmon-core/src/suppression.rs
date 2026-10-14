//! Maintenance suppression rules, paths and freshness
//! (`service_layer/notification_suppression.py`), PLAN.md section 5.4.
//!
//! The fetch itself (redirects, caps, compression refusal) is I/O and lives
//! in `nyxmon-checks`; [`notification_suppression_details`] takes it as a
//! closure that returns the decoded payload, or `None` for any fetch
//! failure (Python fails open on every exception of `_fetch_payload`).

use num_bigint::BigInt;
use num_traits::ToPrimitive;

use crate::pyjson::{
    CmpOp, PyDict, PyJson, PyNumError, json_kind, py_cmp, py_float, py_float_of_int, py_int,
    py_int_of, py_str, py_strip, py_truthy,
};

/// `MAX_SUPPRESSION_BODY_BYTES`.
pub const MAX_SUPPRESSION_BODY_BYTES: usize = 256 * 1024;
/// `MAX_SUPPRESSION_REDIRECTS`.
pub const MAX_SUPPRESSION_REDIRECTS: usize = 5;

static NONE: PyJson = PyJson::Null;

/// `_lookup`: the value at `path`, `Ok(None)` when the path does not exist
/// (Python's `MISSING`, distinct from a JSON `null`).
///
/// `"$"` is the payload; otherwise every `"$."` is removed and the rest is
/// split on `.` (empty parts dropped). A dict key wins; a list index needs
/// an all-digit part (ASCII only, PLAN.md D-6).
///
/// # Errors
/// The `ValueError` of `int(part)` for an index part over CPython's
/// 4300-digit limit, which Python lets escape.
pub fn lookup<'a>(payload: &'a PyJson, path: &str) -> Result<Option<&'a PyJson>, PyNumError> {
    if path == "$" {
        return Ok(Some(payload));
    }
    let cleaned = path.replace("$.", "");
    let mut current = payload;
    for part in cleaned.split('.').filter(|part| !part.is_empty()) {
        current = match current {
            PyJson::Dict(map) if map.contains_key(part) => &map[part],
            PyJson::List(items) if part.bytes().all(|byte| byte.is_ascii_digit()) => {
                // An index beyond `usize` is out of range like any other.
                let index = py_int(part)?.to_usize().unwrap_or(usize::MAX);
                match items.get(index) {
                    Some(item) => item,
                    None => return Ok(None),
                }
            }
            _ => return Ok(None),
        };
    }
    Ok(Some(current))
}

/// `_resolve_path`: [`lookup`] with a missing path read as `null`.
///
/// # Errors
/// See [`lookup`].
pub fn resolve_path<'a>(payload: &'a PyJson, path: &str) -> Result<&'a PyJson, PyNumError> {
    Ok(lookup(payload, path)?.unwrap_or(&NONE))
}

/// `str(value or default)` for a config value.
fn str_or(value: Option<&PyJson>, default: &str) -> String {
    match value {
        Some(value) if py_truthy(value) => py_str(value),
        _ => default.to_owned(),
    }
}

/// `_rule_matches`: true only when the rule positively matches a present
/// value of the same JSON kind; every doubt (unknown operator, missing
/// path, kind mismatch, unsupported comparison) is "no match".
///
/// # Errors
/// See [`lookup`] (Python resolves the path outside its `try`).
pub fn rule_matches(payload: &PyJson, rule: &PyDict) -> Result<bool, PyNumError> {
    let Some(op) = CmpOp::from_symbol(&str_or(rule.get("op"), "")) else {
        return Ok(false);
    };
    let Some(actual) = lookup(payload, &str_or(rule.get("path"), ""))? else {
        return Ok(false);
    };
    let expected = rule.get("value").unwrap_or(&NONE);
    if json_kind(actual) != json_kind(expected) {
        return Ok(false);
    }
    Ok(py_cmp(op, actual, expected).unwrap_or(false))
}

/// `_int_or_none(value)`: Python `int()` of a JSON value, `None` on any
/// `TypeError`/`ValueError`/`OverflowError`.
pub fn int_or_none(value: &PyJson) -> Option<BigInt> {
    py_int_of(value).ok()
}

/// `_finite_seconds` of an `int` (the overflow of `float()` reads as `None`).
fn finite_seconds_of_int(value: &BigInt) -> Option<f64> {
    let seconds = py_float_of_int(value).ok()?;
    (seconds >= 0.0).then_some(seconds)
}

/// `_finite_seconds`: a finite, non-negative number of seconds, else
/// `None` (bools, non-numbers, NaN, infinities, negatives and integers
/// too large for a float).
pub fn finite_seconds(value: &PyJson) -> Option<f64> {
    match value {
        PyJson::Int(number) => finite_seconds_of_int(&BigInt::from(*number)),
        PyJson::BigInt(number) => finite_seconds_of_int(number),
        // `seconds < 0` is false for -0.0, which is kept.
        PyJson::Float(seconds) if seconds.is_finite() && *seconds >= 0.0 => Some(*seconds),
        _ => None,
    }
}

/// `float(value)` for a JSON value, as `_float_or_default` calls it.
fn py_float_of(value: &PyJson) -> Result<Option<f64>, PyNumError> {
    Ok(match value {
        PyJson::Bool(flag) => Some(f64::from(u8::from(*flag))),
        PyJson::Int(number) => Some(*number as f64),
        // `float(int)` raises `OverflowError`, which `_float_or_default`
        // does not catch.
        PyJson::BigInt(number) => Some(py_float_of_int(number)?),
        PyJson::Float(number) => Some(*number),
        PyJson::Str(text) => py_float(text).ok(),
        PyJson::Null | PyJson::List(_) | PyJson::Dict(_) => None,
    })
}

/// `_timeout_seconds(config.get("timeout", 3.0))`: non-numeric or `<= 0`
/// → 3, capped at 30 (`min` keeps a NaN, like Python).
///
/// # Errors
/// `OverflowError` for an integer too large for a float: Python lets it
/// escape `notification_suppression_details`.
pub fn timeout_seconds(value: Option<&PyJson>) -> Result<f64, PyNumError> {
    let timeout = match value {
        None => 3.0,
        Some(value) => py_float_of(value)?.unwrap_or(3.0),
    };
    if timeout <= 0.0 {
        return Ok(3.0);
    }
    Ok(if 30.0 < timeout { 30.0 } else { timeout })
}

/// What the I/O layer must fetch for a check with suppression configured.
#[derive(Clone, Debug, PartialEq)]
pub struct SuppressionRequest {
    /// `str(config["url"]).strip()`, never empty.
    pub url: String,
    /// Seconds, see [`timeout_seconds`].
    pub timeout: f64,
    /// `_build_auth`: basic auth when both are strings.
    pub auth: Option<(String, String)>,
}

/// The `notification_suppression` object of a check's data, if any.
fn suppression_config(data: &PyJson) -> Option<&PyDict> {
    data.get("notification_suppression")?.as_dict()
}

/// The pre-fetch part of `notification_suppression_details`: `None` when
/// suppression is not configured (a non-object `data` included, PLAN.md
/// D-11).
///
/// # Errors
/// See [`timeout_seconds`].
pub fn suppression_request(data: &PyJson) -> Result<Option<SuppressionRequest>, PyNumError> {
    let Some(config) = suppression_config(data) else {
        return Ok(None);
    };
    let url = py_strip(&str_or(config.get("url"), "")).to_owned();
    if url.is_empty() {
        return Ok(None);
    }
    let timeout = timeout_seconds(config.get("timeout"))?;
    let auth = match config.get("auth") {
        Some(PyJson::Dict(auth)) => match (auth.get("username"), auth.get("password")) {
            (Some(PyJson::Str(username)), Some(PyJson::Str(password))) => {
                Some((username.clone(), password.clone()))
            }
            _ => None,
        },
        _ => None,
    };
    Ok(Some(SuppressionRequest { url, timeout, auth }))
}

/// The post-fetch part of `notification_suppression_details`: the
/// freshness guard, `active_statuses`, `active_if` and `active_for_seconds`.
///
/// # Errors
/// See [`lookup`].
pub fn evaluate(
    config: &PyDict,
    url: &str,
    payload: &PyJson,
    now: i64,
) -> Result<Option<PyDict>, PyNumError> {
    // A stale (or unusable) freshness reading suppresses nothing.
    let freshness_path = py_strip(&str_or(config.get("freshness_path"), "")).to_owned();
    if !freshness_path.is_empty() {
        let max_age = config
            .get("freshness_max_seconds")
            .and_then(int_or_none)
            .and_then(|max| finite_seconds_of_int(&max));
        let age = finite_seconds(resolve_path(payload, &freshness_path)?);
        match (age, max_age) {
            (Some(age), Some(max_age)) if max_age > 0.0 && age <= max_age => {}
            _ => return Ok(None),
        }
    }

    let status_path = str_or(config.get("status_path"), "$.last_status");
    let status = resolve_path(payload, &status_path)?;
    let reason = |default: &str| PyJson::Str(str_or(config.get("reason"), default));
    let details = |default: &str| {
        let mut details = PyDict::new();
        details.insert("reason".to_owned(), reason(default));
        details.insert("source_url".to_owned(), PyJson::from(url));
        details.insert("source_status".to_owned(), status.clone());
        details
    };

    let default_statuses = [PyJson::from("running")];
    let active_statuses = match config.get("active_statuses") {
        None => &default_statuses[..],
        Some(PyJson::List(statuses)) => statuses.as_slice(),
        Some(_) => &default_statuses[..],
    };
    if let PyJson::Str(text) = status
        && active_statuses
            .iter()
            .any(|item| item.as_str() == Some(text))
    {
        return Ok(Some(details("maintenance_active")));
    }

    if let Some(PyJson::List(rules)) = config.get("active_if") {
        for rule in rules {
            let Some(rule) = rule.as_dict() else {
                continue;
            };
            if rule_matches(payload, rule)? {
                let mut matched = PyDict::new();
                for key in ["path", "op", "value"] {
                    matched.insert(key.to_owned(), rule.get(key).unwrap_or(&NONE).clone());
                }
                let mut details = details("maintenance_active");
                details.insert("matched_rule".to_owned(), PyJson::Dict(matched));
                return Ok(Some(details));
            }
        }
    }

    let active_for = config.get("active_for_seconds").and_then(int_or_none);
    let finished_path = str_or(
        config.get("finished_epoch_path"),
        "$.last_run_finished_epoch",
    );
    let finished = int_or_none(resolve_path(payload, &finished_path)?);
    let (Some(active_for), Some(finished)) = (active_for, finished) else {
        return Ok(None);
    };
    let age = BigInt::from(now) - &finished;
    if active_for > BigInt::ZERO && age >= BigInt::ZERO && age <= active_for {
        let mut details = details("maintenance_recently_finished");
        details.insert("finished_epoch".to_owned(), PyJson::from_bigint(finished));
        details.insert(
            "active_for_seconds".to_owned(),
            PyJson::from_bigint(active_for),
        );
        return Ok(Some(details));
    }
    Ok(None)
}

/// `notification_suppression_details(check, now_epoch=now)`.
///
/// `fetch` performs the request and returns the decoded payload, or `None`
/// on any failure (fail open).
///
/// # Errors
/// See [`timeout_seconds`] and [`lookup`].
pub fn notification_suppression_details(
    data: &PyJson,
    now: i64,
    fetch: impl FnOnce(&SuppressionRequest) -> Option<PyJson>,
) -> Result<Option<PyDict>, PyNumError> {
    let Some(request) = suppression_request(data)? else {
        return Ok(None);
    };
    let Some(payload) = fetch(&request) else {
        return Ok(None);
    };
    let config = suppression_config(data).expect("checked by suppression_request");
    evaluate(config, &request.url, &payload, now)
}
