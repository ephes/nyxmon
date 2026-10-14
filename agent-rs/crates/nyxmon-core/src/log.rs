//! Log output of the pure logic.
//!
//! Pure code reports operator-facing lines through a [`LogSink`] instead of
//! calling `tracing` directly, so fixtures can compare the exact text and the
//! Python logger name (PLAN.md section 1.7). [`TracingSink`] forwards to
//! `tracing` with the Python logger name as the target.

use std::sync::Mutex;

/// Python logger names used by the ported modules.
pub mod loggers {
    pub const REPOSITORY_INTERFACE: &str = "nyxmon.adapters.repositories.interface";
    pub const COLLECTOR: &str = "nyxmon.adapters.collector";
    pub const NOTIFICATION_POLICY: &str = "nyxmon.service_layer.notification_policy";
    pub const HANDLERS: &str = "nyxmon.service_layer.handlers";
    pub const SITE_CONNECTIVITY: &str = "nyxmon.adapters.site_connectivity";
    pub const NOTIFICATION: &str = "nyxmon.adapters.notification";
    pub const EXECUTOR_TLS: &str = "nyxmon.adapters.runner.executors.tls";
}

/// Receiver of warning lines emitted by pure code.
pub trait LogSink: Send + Sync {
    /// Emit one warning under the Python logger name `logger`.
    fn warning(&self, logger: &'static str, message: &str);
}

/// Forwards warnings to `tracing`, using the Python logger name as target.
#[derive(Debug, Default, Clone, Copy)]
pub struct TracingSink;

impl LogSink for TracingSink {
    fn warning(&self, logger: &'static str, message: &str) {
        // `tracing` needs a constant target per call site.
        use loggers::*;
        match logger {
            REPOSITORY_INTERFACE => {
                tracing::warn!(target: "nyxmon.adapters.repositories.interface", "{message}")
            }
            COLLECTOR => tracing::warn!(target: "nyxmon.adapters.collector", "{message}"),
            NOTIFICATION_POLICY => {
                tracing::warn!(target: "nyxmon.service_layer.notification_policy", "{message}")
            }
            HANDLERS => tracing::warn!(target: "nyxmon.service_layer.handlers", "{message}"),
            SITE_CONNECTIVITY => {
                tracing::warn!(target: "nyxmon.adapters.site_connectivity", "{message}")
            }
            NOTIFICATION => tracing::warn!(target: "nyxmon.adapters.notification", "{message}"),
            EXECUTOR_TLS => {
                tracing::warn!(target: "nyxmon.adapters.runner.executors.tls", "{message}")
            }
            _ => tracing::warn!(logger, "{message}"),
        }
    }
}

/// Collects warnings in memory (tests).
#[derive(Debug, Default)]
pub struct RecordingSink {
    lines: Mutex<Vec<(String, String)>>,
}

impl RecordingSink {
    /// Remove and return the recorded `(logger, message)` pairs.
    pub fn take(&self) -> Vec<(String, String)> {
        std::mem::take(&mut *self.lines.lock().unwrap_or_else(|e| e.into_inner()))
    }
}

impl LogSink for RecordingSink {
    fn warning(&self, logger: &'static str, message: &str) {
        self.lines
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push((logger.to_owned(), message.to_owned()));
    }
}
