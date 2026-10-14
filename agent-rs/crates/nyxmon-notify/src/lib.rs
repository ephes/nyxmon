//! Notifiers of the nyxmon agent (`adapters/notification.py`, PLAN.md 5.11).
//!
//! - [`telegram::TelegramNotifier`]: the MarkdownV2 alert with plain-text
//!   fallback and token redaction, plus the OpsGate ticket integration
//!   ([`opsgate`]) whose outcome is appended to the message.
//! - [`logging::LoggingNotifier`]: used without `--enable-telegram`; in
//!   `--shadow` mode its line is machine-readable.
//!
//! The pure parts (message text, MarkdownV2 escaping, the OpsGate payload and
//! prompt, sanitizing of endpoint data) are pinned byte for byte by the
//! Python-generated fixtures `telegram.json` and `opsgate.json`.

use std::fmt;
use std::future::Future;
use std::pin::Pin;

use nyxmon_core::model::{Check, CheckResult};

mod http;
pub mod log;
pub mod logging;
pub mod markdown;
pub mod opsgate;
pub mod pystr;
pub mod sanitize;
pub mod telegram;

#[cfg(test)]
mod tests;

/// The outcome of one notification (`True`/`False`/`None` of
/// `Notifier.notify_check_failed`).
///
/// Only [`Delivery::Failed`] counts as a delivery failure for the callers
/// (`handlers.py:add_check_result` acknowledges unless the result is a
/// literal `False`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Delivery {
    Delivered,
    Failed,
    /// The notifier never attempted a send (no credentials).
    Unknown,
}

impl Delivery {
    /// `delivered is False`.
    pub fn is_failure(self) -> bool {
        self == Delivery::Failed
    }
}

/// A Python exception that escapes `notify_check_failed`.
///
/// The only one is the `OverflowError` of an `OPSGATE_TICKET_EXPIRES_SECONDS`
/// so large that the ticket expiry leaves Python's `datetime` range: it is
/// raised before the OpsGate submit and before the Telegram send, so the
/// caller's "result handling failed" path runs instead of a delivery
/// outcome.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NotifyRaised {
    /// The Python exception class.
    pub class: &'static str,
    pub message: String,
}

impl fmt::Display for NotifyRaised {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.class, self.message)
    }
}

impl std::error::Error for NotifyRaised {}

/// The future of [`Notifier::notify_check_failed`].
pub type NotifyFuture<'a> =
    Pin<Box<dyn Future<Output = Result<Delivery, NotifyRaised>> + Send + 'a>>;

/// `notification.py:Notifier` (only `notify_check_failed`; the service status
/// notification is never emitted by a production path, PLAN.md 1.2).
pub trait Notifier: Send + Sync {
    /// Notify about a failed or warning check result.
    fn notify_check_failed<'a>(
        &'a self,
        check: &'a Check,
        result: &'a CheckResult,
    ) -> NotifyFuture<'a>;
}
