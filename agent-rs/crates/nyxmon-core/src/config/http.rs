//! `domain/http_config.py:HttpCheckConfig` (`from_dict` + `validate`).

use crate::pyjson::{PyDict, PyJson};

use super::get_or;

/// `DEFAULT_RETRY_STATUS_CODES`.
pub const DEFAULT_RETRY_STATUS_CODES: [i64; 3] = [502, 503, 504];

/// A validated HTTP check configuration (`http` and `json-http`).
#[derive(Clone, Debug, PartialEq)]
pub struct HttpConfig {
    /// Seconds, `0 < timeout <= 300`.
    pub timeout: f64,
    /// `0..=100`.
    pub retries: u32,
    /// Seconds, `0 <= retry_delay <= 300`.
    pub retry_delay: f64,
    pub retry_status_codes: Vec<i64>,
    pub follow_redirects: bool,
    pub expected_status: Option<i64>,
    pub expected_location: Option<String>,
}

impl HttpConfig {
    /// `HttpCheckConfig.from_dict(data)` followed by `validate()`.
    ///
    /// # Errors
    /// The `ValueError` message Python raises; this config raises nothing
    /// else for any JSON input.
    pub fn from_data(data: &PyJson) -> Result<HttpConfig, String> {
        let Some(map) = data.as_dict() else {
            return Err("check.data must be an object".to_owned());
        };
        let codes = match map.get("retry_status_codes") {
            None => DEFAULT_RETRY_STATUS_CODES
                .iter()
                .map(|code| PyJson::Int(*code))
                .collect(),
            Some(PyJson::List(items)) => items.clone(),
            Some(_) => {
                return Err("retry_status_codes must be a list of HTTP status codes".to_owned());
            }
        };
        let follow_redirects = match map.get("follow_redirects") {
            None => true,
            Some(PyJson::Bool(flag)) => *flag,
            Some(_) => return Err("follow_redirects must be a boolean".to_owned()),
        };
        let expected_location = match map.get("expected_location") {
            None | Some(PyJson::Null) => None,
            Some(PyJson::Str(text)) => Some(text.clone()),
            Some(_) => return Err("expected_location must be a string".to_owned()),
        };
        validate(map, &codes, follow_redirects, expected_location)
    }
}

/// A duration accepted by `validate`: `type(x) in (int, float)` (so not a
/// bool), inside `0..=300` and finite.
fn duration(value: &PyJson) -> Option<f64> {
    match value {
        PyJson::Int(number) if (0..=300).contains(number) => Some(*number as f64),
        PyJson::Float(number) if number.is_finite() && (0.0..=300.0).contains(number) => {
            Some(*number)
        }
        _ => None,
    }
}

/// `type(x) is int and low <= x <= high`.
fn int_in(value: &PyJson, low: i64, high: i64) -> Option<i64> {
    match value {
        PyJson::Int(number) if (low..=high).contains(number) => Some(*number),
        _ => None,
    }
}

fn validate(
    map: &PyDict,
    codes: &[PyJson],
    follow_redirects: bool,
    expected_location: Option<String>,
) -> Result<HttpConfig, String> {
    let (ten, two, zero) = (PyJson::Float(10.0), PyJson::Float(2.0), PyJson::Int(0));
    let timeout = duration(get_or(map, "timeout", &ten))
        .filter(|t| *t != 0.0)
        .ok_or_else(|| {
            "timeout must be a finite number greater than zero and between 0 and 300 seconds"
                .to_owned()
        })?;
    let retry_delay = duration(get_or(map, "retry_delay", &two)).ok_or_else(|| {
        "retry_delay must be a finite number between 0 and 300 seconds".to_owned()
    })?;
    let retries = int_in(get_or(map, "retries", &zero), 0, 100)
        .ok_or_else(|| "retries must be an integer between 0 and 100".to_owned())?;
    let retry_status_codes = codes
        .iter()
        .map(|code| int_in(code, 100, 599))
        .collect::<Option<Vec<i64>>>()
        .ok_or_else(|| "retry_status_codes must contain integer HTTP status codes".to_owned())?;
    let expected_status = match map.get("expected_status") {
        None | Some(PyJson::Null) => None,
        Some(value) => Some(
            int_in(value, 100, 599)
                .ok_or_else(|| "expected_status must be an integer HTTP status code".to_owned())?,
        ),
    };
    if let Some(location) = &expected_location {
        if location.is_empty() {
            return Err("expected_location must not be empty".to_owned());
        }
        if follow_redirects {
            return Err("expected_location requires follow_redirects to be false".to_owned());
        }
    }
    Ok(HttpConfig {
        timeout,
        retries: u32::try_from(retries).unwrap_or(0),
        retry_delay,
        retry_status_codes,
        follow_redirects,
        expected_status,
        expected_location,
    })
}
