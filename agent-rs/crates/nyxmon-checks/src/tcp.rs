//! The TCP check (`executors/tcp_executor.py`). This part holds the
//! structured errors shared by the connection steps; the executor itself
//! follows in the next part of slice S6.

use nyxmon_core::pyjson::PyDict;

/// `TcpCheckError`: a structured failure of one attempt.
#[derive(Clone, Debug, PartialEq)]
pub struct TcpCheckError {
    pub error_type: &'static str,
    pub message: String,
    pub retryable: bool,
    /// Extra result keys, appended after the standard ones.
    pub data: PyDict,
}

impl TcpCheckError {
    pub fn new(error_type: &'static str, message: String, retryable: bool) -> Self {
        Self::with_data(error_type, message, retryable, PyDict::new())
    }

    pub fn with_data(
        error_type: &'static str,
        message: String,
        retryable: bool,
        data: PyDict,
    ) -> Self {
        TcpCheckError {
            error_type,
            message,
            retryable,
            data,
        }
    }
}

/// Why one attempt failed.
#[derive(Clone, Debug, PartialEq)]
pub enum AttemptError {
    /// A `TcpCheckError`.
    Check(TcpCheckError),
    /// Any other exception: `unexpected_error` with `str(exc)`, no retry.
    Unexpected(String),
}

impl From<TcpCheckError> for AttemptError {
    fn from(err: TcpCheckError) -> Self {
        AttemptError::Check(err)
    }
}
