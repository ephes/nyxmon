//! `domain/json_metrics_config.py:JsonMetricsCheckConfig`
//! (`from_dict` + `validate`).
//!
//! The Python config keeps most values untyped, so several malformed inputs
//! pass `from_dict`/`validate` and fail later in the executor (a non-string
//! `url` or `auth` member, a non-string check `path`). The fields that can
//! carry such values stay raw [`PyJson`] here and the executor reproduces
//! those failures at the point Python raises them.

use num_bigint::BigInt;
use num_traits::{ToPrimitive, Zero};

use super::{ConfigError, PyException, get_or, sorted_set_repr, str_set_contains};
use crate::pyjson::{CmpOp, PyJson, py_float, py_int_of, py_truthy};

/// `ALLOWED_OPERATORS`.
pub const ALLOWED_OPERATORS: [&str; 6] = ["<", "<=", ">", ">=", "==", "!="];
/// `ALLOWED_SEVERITIES`.
pub const ALLOWED_SEVERITIES: [&str; 2] = ["warning", "critical"];
/// `DEFAULT_MAX_BODY_BYTES`.
pub const DEFAULT_MAX_BODY_BYTES: i64 = 1024 * 1024;

/// A threshold severity.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Severity {
    Warning,
    Critical,
}

impl Severity {
    pub fn as_str(self) -> &'static str {
        match self {
            Severity::Warning => "warning",
            Severity::Critical => "critical",
        }
    }
}

/// `JsonMetricsCheckConfig.Check`.
#[derive(Clone, Debug, PartialEq)]
pub struct MetricCheck {
    /// Any JSON value; only a `str` path can be resolved (a non-string path
    /// raises `TypeError` during evaluation, as in Python).
    pub path: PyJson,
    pub op: CmpOp,
    pub value: PyJson,
    pub severity: Severity,
}

/// A validated json-metrics configuration.
#[derive(Clone, Debug, PartialEq)]
pub struct JsonMetricsConfig {
    /// Truthy, but of any JSON type (the executor fails on a non-string).
    pub url: PyJson,
    pub checks: Vec<MetricCheck>,
    /// Seconds; positive, possibly `inf` (a huge integer), `NaN` passes too.
    pub timeout: f64,
    /// The timeout is an integer too large for a float: httpx fails with
    /// `OverflowError` when it first uses it (every fetch attempt).
    pub timeout_overflows: bool,
    /// `None` or a value containing `"username"` and `"password"` (a dict,
    /// list or string, as Python's `in` allows).
    pub auth: Option<PyJson>,
    /// `int(data["retries"])`, `>= 0`.
    pub retries: BigInt,
    /// `float(data["retry_delay"])`, `>= 0` or NaN.
    pub retry_delay: f64,
    /// Positive.
    pub max_body_bytes: BigInt,
}

impl JsonMetricsConfig {
    /// `JsonMetricsCheckConfig.from_dict(data)` followed by `validate()`.
    ///
    /// # Errors
    /// [`ConfigError::Invalid`] for the `ValueError`s (the executor reports
    /// `configuration_error`), [`ConfigError::Exception`] for any other
    /// exception (the runner reports `executor_exception`).
    pub fn from_data(data: &PyJson) -> Result<JsonMetricsConfig, ConfigError> {
        let Some(map) = data.as_dict() else {
            return Err(PyException::ATTRIBUTE_ERROR.into());
        };
        let empty = PyJson::Str(String::new());
        let url = get_or(map, "url", &empty);
        if !py_truthy(url) {
            return Err(ConfigError::invalid("url is required"));
        }
        let no_checks = PyJson::List(Vec::new());
        let raw_checks = get_or(map, "checks", &no_checks);
        if !py_truthy(raw_checks) {
            return Err(ConfigError::invalid(
                "checks must contain at least one entry",
            ));
        }
        let checks = parse_checks(raw_checks)?;

        let auth = match map.get("auth") {
            None | Some(PyJson::Null) => None,
            Some(auth) => {
                if !contains(auth, "username")? || !contains(auth, "password")? {
                    return Err(ConfigError::invalid(
                        "auth must include username and password",
                    ));
                }
                Some(auth.clone())
            }
        };

        let max_body_bytes = match map.get("max_body_bytes") {
            None | Some(PyJson::Null) => BigInt::from(DEFAULT_MAX_BODY_BYTES),
            Some(PyJson::Int(number)) => BigInt::from(*number),
            Some(PyJson::BigInt(number)) => number.clone(),
            Some(_) => {
                return Err(ConfigError::invalid(
                    "max_body_bytes must be a whole number of bytes",
                ));
            }
        };

        let (one, two, ten) = (PyJson::Int(1), PyJson::Float(2.0), PyJson::Float(10.0));
        let retries = py_int_of(get_or(map, "retries", &one)).map_err(num_error)?;
        let retry_delay = py_float_of(get_or(map, "retry_delay", &two))?;
        let timeout = get_or(map, "timeout", &ten);

        // validate()
        let timeout = match timeout_seconds(timeout) {
            Some(seconds) if seconds <= 0.0 => {
                return Err(ConfigError::invalid("timeout must be positive"));
            }
            Some(seconds) => seconds,
            None => {
                return Err(PyException::TYPE_ERROR.into());
            }
        };
        if retries < BigInt::zero() {
            return Err(ConfigError::invalid("retries must be zero or positive"));
        }
        if retry_delay < 0.0 {
            return Err(ConfigError::invalid("retry_delay must be zero or positive"));
        }
        if max_body_bytes <= BigInt::zero() {
            return Err(ConfigError::invalid("max_body_bytes must be positive"));
        }
        if checks
            .iter()
            .any(|check| matches!(&check.path, PyJson::Str(path) if path.is_empty()))
        {
            return Err(ConfigError::invalid("check.path is required"));
        }

        let timeout_overflows = matches!(get_or(map, "timeout", &ten), PyJson::BigInt(n)
            if !n.to_f64().is_some_and(f64::is_finite));
        Ok(JsonMetricsConfig {
            url: url.clone(),
            checks,
            timeout,
            timeout_overflows,
            auth,
            retries,
            retry_delay,
            max_body_bytes,
        })
    }
}

/// The entries `for entry in raw_checks` iterates over, or the exception.
fn parse_checks(raw: &PyJson) -> Result<Vec<MetricCheck>, ConfigError> {
    let entries: &[PyJson] = match raw {
        PyJson::List(items) => items,
        // Iterating a dict yields its keys, a string its characters: both
        // are strings, whose missing `.get` raises AttributeError.
        PyJson::Dict(_) | PyJson::Str(_) => {
            return Err(PyException::ATTRIBUTE_ERROR.into());
        }
        _ => {
            return Err(PyException::TYPE_ERROR.into());
        }
    };
    let mut checks = Vec::with_capacity(entries.len());
    let (null, empty) = (PyJson::Null, PyJson::Str(String::new()));
    for entry in entries {
        let Some(map) = entry.as_dict() else {
            return Err(PyException::ATTRIBUTE_ERROR.into());
        };
        let op = get_or(map, "op", &null);
        let severity = get_or(map, "severity", &null);
        if !str_set_contains(op, &ALLOWED_OPERATORS)? {
            return Err(ConfigError::invalid(format!(
                "op must be one of {}",
                sorted_set_repr(&ALLOWED_OPERATORS)
            )));
        }
        if !str_set_contains(severity, &ALLOWED_SEVERITIES)? {
            return Err(ConfigError::invalid(format!(
                "severity must be one of {}",
                sorted_set_repr(&ALLOWED_SEVERITIES)
            )));
        }
        let op = op
            .as_str()
            .and_then(CmpOp::from_symbol)
            .expect("op is an allowed operator");
        let severity = if severity.as_str() == Some("critical") {
            Severity::Critical
        } else {
            Severity::Warning
        };
        checks.push(MetricCheck {
            path: get_or(map, "path", &empty).clone(),
            op,
            value: get_or(map, "value", &null).clone(),
            severity,
        });
    }
    Ok(checks)
}

/// `needle in value` for the `auth` membership test.
fn contains(value: &PyJson, needle: &str) -> Result<bool, PyException> {
    match value {
        PyJson::Dict(map) => Ok(map.contains_key(needle)),
        PyJson::List(items) => Ok(items.iter().any(|item| item.as_str() == Some(needle))),
        PyJson::Str(text) => Ok(text.contains(needle)),
        _ => Err(PyException::TYPE_ERROR),
    }
}

/// `timeout` as seconds when it is a number or bool (Python compares it with
/// `0`); `None` when the comparison raises `TypeError`.
fn timeout_seconds(value: &PyJson) -> Option<f64> {
    match value {
        PyJson::Bool(flag) => Some(f64::from(u8::from(*flag))),
        PyJson::Int(number) => Some(*number as f64),
        PyJson::BigInt(number) => Some(number.to_f64().unwrap_or(f64::INFINITY)),
        PyJson::Float(number) => Some(*number),
        _ => None,
    }
}

fn num_error(err: crate::pyjson::PyNumError) -> ConfigError {
    match err {
        crate::pyjson::PyNumError::ValueError(message) => ConfigError::Invalid(message),
        other => PyException(other.python_class()).into(),
    }
}

/// `float(value)` for a JSON value.
fn py_float_of(value: &PyJson) -> Result<f64, ConfigError> {
    match value {
        PyJson::Bool(flag) => Ok(f64::from(u8::from(*flag))),
        PyJson::Int(number) => Ok(*number as f64),
        PyJson::BigInt(number) => match number.to_f64() {
            Some(float) if float.is_finite() => Ok(float),
            _ => Err(PyException::OVERFLOW_ERROR.into()),
        },
        PyJson::Float(number) => Ok(*number),
        PyJson::Str(text) => py_float(text).map_err(num_error),
        _ => Err(PyException::TYPE_ERROR.into()),
    }
}
