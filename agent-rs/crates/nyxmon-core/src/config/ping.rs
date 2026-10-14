//! Ping check configuration (`domain/ping_config.py`) and the executor's
//! target host (`normalize_ping_target`, `PingCheckExecutor._target_host`).

use super::ConfigOutcome;
use crate::pyjson::{PyDict, PyJson, py_float, py_float_of_int, py_isspace, py_strip};
use crate::pyurl::urlsplit_hostname;

/// The message of a check without a usable target.
pub const TARGET_REQUIRED: &str =
    "a host name or IP address is required (set check.url or data.host)";

/// `PingCheckConfig.MAX_COUNT`.
pub const MAX_COUNT: i64 = 20;
/// `PingCheckConfig.MAX_TIMEOUT`.
pub const MAX_TIMEOUT: f64 = 60.0;
/// `PingCheckConfig.MAX_INTERVAL`.
pub const MAX_INTERVAL: f64 = 60.0;

/// `PingCheckConfig` after `from_dict` + `validate`.
#[derive(Clone, Debug, PartialEq)]
pub struct PingConfig {
    /// Per-attempt reply timeout in seconds (finite, `0 < timeout <= 60`).
    pub timeout: f64,
    /// Number of single-echo attempts (`1..=20`).
    pub count: i64,
    /// Seconds between attempts (finite, `0 <= interval <= 60`; may be
    /// `-0.0`, which Python keeps).
    pub interval: f64,
    /// `data.host`, stripped; `None` when absent, null or blank.
    pub host: Option<String>,
}

/// `_number(data, name, default)`: a finite `float(value)`, rejecting
/// booleans and values `float()` refuses.
fn number(data: &PyDict, name: &str, default: f64) -> Result<f64, String> {
    let not_a_number = || format!("{name} must be a number");
    let parsed = match data.get(name) {
        None => default,
        Some(PyJson::Bool(_)) => return Err(not_a_number()),
        // `float(int)` rounds half to even, as `as` does.
        Some(PyJson::Int(value)) => *value as f64,
        Some(PyJson::BigInt(value)) => py_float_of_int(value).map_err(|_| not_a_number())?,
        Some(PyJson::Float(value)) => *value,
        Some(PyJson::Str(text)) => py_float(text).map_err(|_| not_a_number())?,
        // `float(None)`, `float([])`, `float({})`: TypeError.
        Some(_) => return Err(not_a_number()),
    };
    if !parsed.is_finite() {
        return Err(format!("{name} must be a finite number"));
    }
    Ok(parsed)
}

impl PingConfig {
    /// `PingCheckConfig.from_dict(check.data)` followed by `validate()`.
    ///
    /// Every failure is a `ValueError` the executor reports as
    /// `configuration_error`. A JSON `null` (PLAN.md D-11: the store's
    /// reading of a `null` column) counts as `{}`.
    pub fn from_data(data: &PyJson) -> ConfigOutcome<PingConfig> {
        let empty = PyDict::new();
        let data = match data {
            PyJson::Null => &empty,
            PyJson::Dict(map) => map,
            _ => return ConfigOutcome::ConfigError("check data must be an object".to_owned()),
        };
        match Self::parse(data).and_then(Self::validate) {
            Ok(config) => ConfigOutcome::Ok(config),
            Err(message) => ConfigOutcome::ConfigError(message),
        }
    }

    /// `from_dict`; `count` stays a float until `validate` bounds it.
    fn parse(data: &PyDict) -> Result<(f64, f64, f64, Option<String>), String> {
        let count = number(data, "count", 3.0)?;
        if count.fract() != 0.0 {
            return Err("count must be a whole number".to_owned());
        }
        let host = match data.get("host") {
            None | Some(PyJson::Null) => None,
            Some(PyJson::Str(host)) => Some(host),
            Some(_) => return Err("host must be a string".to_owned()),
        };
        let timeout = number(data, "timeout", 5.0)?;
        let interval = number(data, "interval", 1.0)?;
        // `host.strip() or None`.
        let host = host
            .map(|host| py_strip(host))
            .filter(|host| !host.is_empty())
            .map(str::to_owned);
        Ok((timeout, count, interval, host))
    }

    /// `validate()`.
    fn validate(
        (timeout, count, interval, host): (f64, f64, f64, Option<String>),
    ) -> Result<PingConfig, String> {
        if timeout <= 0.0 || timeout > MAX_TIMEOUT {
            return Err("timeout must be greater than 0 and at most 60 seconds".to_owned());
        }
        if count < 1.0 || count > MAX_COUNT as f64 {
            return Err(format!("count must be between 1 and {MAX_COUNT}"));
        }
        if interval < 0.0 || interval > MAX_INTERVAL {
            return Err("interval must be between 0 and 60 seconds".to_owned());
        }
        Ok(PingConfig {
            timeout,
            // A whole number in 1..=20.
            count: count as i64,
            interval,
            host,
        })
    }

    /// `PingCheckExecutor._target_host`: the normalised `data.host`, else
    /// the normalised `check.url`; `None` when it is not a usable target.
    pub fn target_host(&self, url: &str) -> Option<String> {
        normalize_ping_target(self.host.as_deref().unwrap_or(url))
    }

    /// The `base` keys every result past the target check starts with.
    pub fn base_data(&self, host: &str) -> PyDict {
        let mut base = PyDict::new();
        base.insert("host".to_owned(), PyJson::Str(host.to_owned()));
        base.insert("timeout".to_owned(), PyJson::Float(self.timeout));
        base.insert("count".to_owned(), PyJson::Int(self.count));
        base.insert("interval".to_owned(), PyJson::Float(self.interval));
        base
    }
}

/// `normalize_ping_target`: the host of a URL, a bracketed IPv6 literal
/// without brackets, or the bare value; `None` for an empty value, one with
/// whitespace or one starting with `-` (it could be read as a `ping`
/// option).
pub fn normalize_ping_target(value: &str) -> Option<String> {
    let mut candidate = py_strip(value).to_owned();
    if candidate.contains("://") {
        // `urlparse(candidate).hostname or ""`; a `ValueError` rejects it.
        candidate = urlsplit_hostname(&candidate).ok()?.unwrap_or_default();
    } else if candidate.len() >= 2 && candidate.starts_with('[') && candidate.ends_with(']') {
        candidate = candidate[1..candidate.len() - 1].to_owned();
    }
    let candidate = py_strip(&candidate);
    if candidate.is_empty() || candidate.starts_with('-') || candidate.chars().any(py_isspace) {
        return None;
    }
    Some(candidate.to_owned())
}

#[cfg(test)]
mod tests;
