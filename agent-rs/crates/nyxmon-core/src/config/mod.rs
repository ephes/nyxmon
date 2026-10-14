//! Check configurations (`domain/*_config.py`): parsing and validation of
//! `health_check.data` exactly as the Python `from_dict` + `validate`.
//!
//! Each config parser takes the raw `check.data` value (any JSON type,
//! PLAN.md D-11) and reproduces the Python outcome: a valid config, the
//! `ValueError` message the executor reports as `configuration_error`, or
//! another Python exception (`TypeError`, `AttributeError`, ...). The
//! outcomes are pinned by the `config_<type>.json` fixtures.

pub mod dns;
pub mod http;
pub mod imap;
pub mod json_metrics;
pub mod mail;
pub mod ping;
pub mod smtp;
pub mod tcp;

use crate::pyjson::{PyDict, PyJson};

/// A Python exception that escapes an executor; the runner records it as
/// `executor_exception` with the class name as `error_msg`
/// (`async_runner.py:_run_one`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PyException(pub &'static str);

impl PyException {
    pub const TYPE_ERROR: PyException = PyException("TypeError");
    pub const VALUE_ERROR: PyException = PyException("ValueError");
    pub const ATTRIBUTE_ERROR: PyException = PyException("AttributeError");
    pub const OVERFLOW_ERROR: PyException = PyException("OverflowError");
}

/// The outcome of reading a check configuration.
#[derive(Clone, Debug, PartialEq)]
pub enum ConfigOutcome<T> {
    Ok(T),
    /// A `ValueError` the executor turns into `configuration_error`.
    ConfigError(String),
    /// Any other exception, which escapes the executor.
    PyException(PyException),
}

/// The error side of a config parser written with `?`: converts into
/// [`ConfigOutcome`] with [`ConfigError::into_outcome`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ConfigError {
    /// `ValueError(message)`: reported as `configuration_error`.
    Invalid(String),
    /// Any other exception escaping the config code.
    Exception(PyException),
}

impl ConfigError {
    pub fn invalid(message: impl Into<String>) -> Self {
        ConfigError::Invalid(message.into())
    }

    /// The [`ConfigOutcome`] of a parser result.
    pub fn into_outcome<T>(result: Result<T, ConfigError>) -> ConfigOutcome<T> {
        match result {
            Ok(config) => ConfigOutcome::Ok(config),
            Err(ConfigError::Invalid(message)) => ConfigOutcome::ConfigError(message),
            Err(ConfigError::Exception(exc)) => ConfigOutcome::PyException(exc),
        }
    }
}

impl From<PyException> for ConfigError {
    fn from(exc: PyException) -> Self {
        ConfigError::Exception(exc)
    }
}

/// The sorted rendering of a Python `set` of strings (`{'a', 'b'}`), used in
/// messages whose Python form depends on `PYTHONHASHSEED` (PLAN.md D-2).
pub fn sorted_set_repr(items: &[&str]) -> String {
    let mut sorted: Vec<&str> = items.to_vec();
    sorted.sort_unstable();
    let inner: Vec<String> = sorted.iter().map(|s| format!("'{s}'")).collect();
    format!("{{{}}}", inner.join(", "))
}

/// `value in {<str>, ...}` for a set of strings: unhashable values raise
/// `TypeError`, and only an equal `str` is a member.
///
/// # Errors
/// `TypeError` for lists and dicts.
pub fn str_set_contains(value: &PyJson, items: &[&str]) -> Result<bool, PyException> {
    match value {
        PyJson::List(_) | PyJson::Dict(_) => Err(PyException::TYPE_ERROR),
        PyJson::Str(text) => Ok(items.contains(&text.as_str())),
        _ => Ok(false),
    }
}

/// `dict.get(key, default)` on a dict.
pub(crate) fn get_or<'a>(map: &'a PyDict, key: &str, default: &'a PyJson) -> &'a PyJson {
    map.get(key).unwrap_or(default)
}

#[cfg(test)]
mod tests;
