//! Log output of the pure logic.
//!
//! Pure code reports operator-facing lines through a [`LogSink`] instead of
//! calling `tracing` directly, so fixtures can compare the exact text and the
//! Python logger name (PLAN.md section 1.7). [`TracingSink`] forwards to
//! `tracing` with the Python logger name as the target.

use std::collections::{HashSet, VecDeque};
use std::fmt;
use std::num::NonZeroUsize;
use std::sync::{Arc, Mutex};

/// Python logger names used by the ported modules.
pub mod loggers {
    pub const REPOSITORY_INTERFACE: &str = "nyxmon.adapters.repositories.interface";
    pub const COLLECTOR: &str = "nyxmon.adapters.collector";
    pub const NOTIFICATION_POLICY: &str = "nyxmon.service_layer.notification_policy";
    pub const HANDLERS: &str = "nyxmon.service_layer.handlers";
    pub const SITE_CONNECTIVITY: &str = "nyxmon.adapters.site_connectivity";
    pub const NOTIFICATION: &str = "nyxmon.adapters.notification";
    pub const EXECUTOR_TLS: &str = "nyxmon.adapters.runner.executors.tls";
    pub const SITE_DEPENDENCY: &str = "nyxmon.service_layer.site_dependency";
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
            SITE_DEPENDENCY => {
                tracing::warn!(target: "nyxmon.service_layer.site_dependency", "{message}")
            }
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

/// Warn-once bookkeeping of the alerting modules: the module-level
/// `_warned_*` sets of `notification_policy.py`, `site_dependency.py` and
/// `site_connectivity.py`, and the collector's runtime-estimate warning keys.
///
/// A key is `(scope, check_id, field)`; scopes have independent identities but
/// share one bounded FIFO. Recent repeats are suppressed without refreshing
/// their position; an evicted key may warn again (PLAN.md D-24).
pub struct WarnOnce {
    sink: Arc<dyn LogSink>,
    capacity: NonZeroUsize,
    seen: Mutex<WarningKeys>,
}

/// Maximum retained keys across all scopes of a default [`WarnOnce`].
pub const DEFAULT_WARN_ONCE_CAPACITY: NonZeroUsize = NonZeroUsize::new(1_024).unwrap();

type WarningKey = (&'static str, i64, String);

#[derive(Default)]
struct WarningKeys {
    keys: HashSet<WarningKey>,
    order: VecDeque<WarningKey>,
}

impl fmt::Debug for WarnOnce {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("WarnOnce").finish_non_exhaustive()
    }
}

impl WarnOnce {
    pub fn new(sink: Arc<dyn LogSink>) -> Self {
        Self::with_capacity(sink, DEFAULT_WARN_ONCE_CAPACITY)
    }

    /// Use a smaller/larger nonzero FIFO allowance (primarily for testing).
    pub fn with_capacity(sink: Arc<dyn LogSink>, capacity: NonZeroUsize) -> Self {
        WarnOnce {
            sink,
            capacity,
            seen: Mutex::new(WarningKeys::default()),
        }
    }

    /// Emit `message()` under `logger` unless `(scope, check_id, field)`
    /// is still retained. Evict the oldest key when full. Returns whether the
    /// warning was emitted; formatting and delivery happen outside the lock.
    pub fn warn(
        &self,
        logger: &'static str,
        scope: &'static str,
        check_id: i64,
        field: &str,
        message: impl FnOnce() -> String,
    ) -> bool {
        let key = (scope, check_id, field.to_owned());
        let first = {
            let mut seen = self.seen.lock().unwrap_or_else(|e| e.into_inner());
            if seen.keys.contains(&key) {
                false
            } else {
                if seen.order.len() == self.capacity.get() {
                    let oldest = seen.order.pop_front().expect("full FIFO is nonempty");
                    seen.keys.remove(&oldest);
                }
                seen.keys.insert(key.clone());
                seen.order.push_back(key);
                true
            }
        };
        if first {
            self.sink.warning(logger, &message());
        }
        first
    }

    /// Forget every key of `scope` (Python's `reset_*_warning_state`, tests).
    pub fn reset(&self, scope: &'static str) {
        let mut seen = self.seen.lock().unwrap_or_else(|e| e.into_inner());
        seen.keys.retain(|(seen_scope, _, _)| *seen_scope != scope);
        seen.order.retain(|(seen_scope, _, _)| *seen_scope != scope);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn warn(warnings: &WarnOnce, scope: &'static str, check_id: i64, field: &str) -> bool {
        warnings.warn(loggers::NOTIFICATION_POLICY, scope, check_id, field, || {
            format!("{scope}/{check_id}/{field}")
        })
    }

    fn assert_retained(warnings: &WarnOnce, expected: usize) {
        let seen = warnings.seen.lock().unwrap();
        assert_eq!(seen.keys.len(), expected);
        assert_eq!(seen.order.len(), expected);
        assert!(expected <= warnings.capacity.get());
        assert_eq!(
            seen.order.iter().cloned().collect::<HashSet<_>>(),
            seen.keys
        );
    }

    #[test]
    fn small_capacity_bounds_churn_and_keeps_recent_duplicates_lazy() {
        let sink = Arc::new(RecordingSink::default());
        let warnings = WarnOnce::with_capacity(sink.clone(), NonZeroUsize::new(3).unwrap());
        for id in 0..100 {
            assert!(warn(&warnings, "policy", id, "field"));
            assert_retained(&warnings, usize::min(id as usize + 1, 3));
            assert!(
                !warnings.warn(loggers::NOTIFICATION_POLICY, "policy", id, "field", || {
                    panic!("duplicate must not format")
                })
            );
        }
        assert_eq!(sink.take().len(), 100);
        for id in 97..100 {
            assert!(!warn(&warnings, "policy", id, "field"));
        }
        // Repeating 97 did not refresh its FIFO position.
        assert!(warn(&warnings, "policy", 100, "field"));
        assert!(warn(&warnings, "policy", 97, "field"));
        assert_retained(&warnings, 3);
        assert_eq!(sink.take().len(), 2);
    }

    #[test]
    fn scope_and_field_identity_and_reset_preserve_fifo_consistency() {
        let warnings = WarnOnce::with_capacity(
            Arc::new(RecordingSink::default()),
            NonZeroUsize::new(3).unwrap(),
        );
        assert!(warn(&warnings, "policy", 1, "a"));
        assert!(warn(&warnings, "site", 1, "a"));
        assert!(warn(&warnings, "policy", 1, "b"));
        assert_retained(&warnings, 3);
        warnings.reset("policy");
        warnings.reset("absent");
        assert_retained(&warnings, 1);
        assert!(!warn(&warnings, "site", 1, "a"));
        assert!(warn(&warnings, "policy", 1, "a"));
        assert!(warn(&warnings, "policy", 1, "b"));
        assert!(warn(&warnings, "policy", 2, "a"));
        assert_retained(&warnings, 3);
        assert!(warn(&warnings, "site", 1, "a"));
        warnings.reset("policy");
        warnings.reset("site");
        assert_retained(&warnings, 0);
    }

    #[test]
    fn capacity_one_and_concurrent_duplicates_stay_bounded() {
        let sink = Arc::new(RecordingSink::default());
        let warnings = WarnOnce::with_capacity(sink.clone(), NonZeroUsize::new(1).unwrap());
        std::thread::scope(|threads| {
            for _ in 0..16 {
                threads.spawn(|| {
                    warn(&warnings, "policy", 1, "field");
                });
            }
        });
        assert_eq!(sink.take().len(), 1);
        assert_retained(&warnings, 1);
        assert!(warn(&warnings, "policy", 2, "field"));
        assert!(warn(&warnings, "policy", 1, "field"));
        assert_retained(&warnings, 1);
    }

    #[test]
    fn dedup_churn_rewarns_evicted_keys_without_silencing_new_keys() {
        let sink = Arc::new(RecordingSink::default());
        let warnings = WarnOnce::new(sink.clone());
        for check_id in 0..2_048 {
            assert!(warnings.warn(
                loggers::NOTIFICATION_POLICY,
                "policy",
                check_id,
                "field",
                || { format!("check {check_id}") }
            ));
        }
        assert_eq!(sink.take().len(), 2_048);
        assert!(!warnings.warn(
            loggers::NOTIFICATION_POLICY,
            "policy",
            2_047,
            "field",
            || { panic!("recent duplicate must not format") }
        ));
        assert!(
            warnings.warn(loggers::NOTIFICATION_POLICY, "policy", 0, "field", || {
                "evicted key".to_owned()
            })
        );
        assert_eq!(sink.take().len(), 1);
    }
}
