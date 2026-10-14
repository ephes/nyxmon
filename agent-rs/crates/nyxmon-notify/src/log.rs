//! Log output of the notifiers.
//!
//! Every line goes to the Python logger `nyxmon.adapters.notification`
//! (PLAN.md 1.7). The notifiers write through a [`NotifyLog`] so tests can
//! compare the exact lines with the Python fixtures.

use std::sync::Mutex;

/// The Python logger name of `adapters/notification.py`.
pub const LOGGER: &str = nyxmon_core::log::loggers::NOTIFICATION;

/// Log levels the notifiers use.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Level {
    Info,
    Warning,
    Error,
}

impl Level {
    /// The Python `levelname`.
    pub fn as_str(self) -> &'static str {
        match self {
            Level::Info => "INFO",
            Level::Warning => "WARNING",
            Level::Error => "ERROR",
        }
    }
}

/// Receiver of the notifier log lines.
pub trait NotifyLog: Send + Sync {
    fn log(&self, level: Level, message: &str);
}

/// Forwards to `tracing` with target `nyxmon.adapters.notification`.
#[derive(Clone, Copy, Debug, Default)]
pub struct TracingLog;

impl NotifyLog for TracingLog {
    fn log(&self, level: Level, message: &str) {
        match level {
            Level::Info => tracing::info!(target: "nyxmon.adapters.notification", "{message}"),
            Level::Warning => tracing::warn!(target: "nyxmon.adapters.notification", "{message}"),
            Level::Error => tracing::error!(target: "nyxmon.adapters.notification", "{message}"),
        }
    }
}

/// Collects the lines in memory (tests).
#[derive(Debug, Default)]
pub struct RecordingLog {
    lines: Mutex<Vec<(Level, String)>>,
}

impl RecordingLog {
    /// Remove and return the recorded lines.
    pub fn take(&self) -> Vec<(Level, String)> {
        std::mem::take(&mut *self.lines.lock().unwrap_or_else(|e| e.into_inner()))
    }
}

impl NotifyLog for RecordingLog {
    fn log(&self, level: Level, message: &str) {
        self.lines
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push((level, message.to_owned()));
    }
}
