//! Check configurations (`domain/*_config.py`): parsing and validation of
//! `health_check.data` exactly as the Python `from_dict` + `validate`.

pub mod tcp;

/// A Python exception that escapes an executor; the runner records it as
/// `executor_exception` with the class name as `error_msg`
/// (`async_runner.py:_run_one`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PyException(pub &'static str);

impl PyException {
    pub const TYPE_ERROR: PyException = PyException("TypeError");
    pub const VALUE_ERROR: PyException = PyException("ValueError");
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
