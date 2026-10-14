//! The executor registry (`executors/__init__.py:ExecutorRegistry` and
//! `async_runner.py:_preregister_executors` / `_register_executors`).
//!
//! The registered check types and their order are user-visible: the
//! `unknown_check_type` message lists them in registration order
//! (PLAN.md 1.6), and startup validation compares persisted checks against
//! them (`startup_validation.py`).

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use nyxmon_core::config::PyException;
use nyxmon_core::model::{Check, CheckResult};

use crate::ctx::BatchCtx;
use crate::dns::DnsSettings;
use crate::mail::SystemMailEnv;
use crate::ping::PingExecutor;
use crate::tcp::TcpExecutor;

/// The check types in Python's registration order.
pub const REGISTERED_TYPES: [&str; 8] = [
    "http",
    "json-http",
    "dns",
    "tcp",
    "json-metrics",
    "imap",
    "smtp",
    "ping",
];

/// `str(UnknownCheckTypeError(check_type, registered))`.
pub fn unknown_check_type_message(check_type: &str, registered: &[&str]) -> String {
    let listed = if registered.is_empty() {
        "none".to_owned()
    } else {
        registered.join(", ")
    };
    format!("No executor registered for check type '{check_type}'. Registered types: {listed}")
}

/// One execution: the result, or the Python exception that would escape
/// the executor (the runner records it as `executor_exception`).
pub type ExecFuture = Pin<Box<dyn Future<Output = Result<CheckResult, PyException>> + Send>>;

/// What the runner executes checks with.
pub trait Executors: Send + Sync + 'static {
    /// `list_registered_types()`, in registration order.
    fn registered_types(&self) -> &[&'static str];

    /// Start executing `check`, or `None` when no executor is registered for
    /// its type. `batch` is shared by the checks of one batch.
    fn execute(&self, check: Check, batch: Arc<BatchCtx>) -> Option<ExecFuture>;
}

/// The production executors.
#[derive(Clone, Default)]
pub struct Registry {
    tcp: TcpExecutor,
    ping: PingExecutor,
    dns: DnsSettings,
}

impl Registry {
    pub fn new() -> Self {
        Registry::default()
    }
}

impl Executors for Registry {
    fn registered_types(&self) -> &[&'static str] {
        &REGISTERED_TYPES
    }

    fn execute(&self, check: Check, batch: Arc<BatchCtx>) -> Option<ExecFuture> {
        let future: ExecFuture = match check.check_type.as_str() {
            "http" | "json-http" => {
                Box::pin(async move { Ok(crate::http::execute(&check, &batch).await) })
            }
            "dns" => {
                let settings = self.dns.clone();
                Box::pin(async move { Ok(crate::dns::execute(&check, &settings).await) })
            }
            "tcp" => {
                let tcp = self.tcp.clone();
                Box::pin(async move { tcp.execute(&check).await })
            }
            "json-metrics" => {
                Box::pin(async move { crate::json_metrics::execute(&check, &batch).await })
            }
            "imap" => Box::pin(async move { crate::imap::execute(&check, &SystemMailEnv).await }),
            "smtp" => {
                Box::pin(async move { Ok(crate::smtp::execute(&check, &SystemMailEnv).await) })
            }
            "ping" => {
                let ping = self.ping.clone();
                Box::pin(async move { ping.execute(&check).await })
            }
            _ => return None,
        };
        Some(future)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use nyxmon_core::PyJson;

    fn check(check_type: &str) -> Check {
        Check {
            check_id: 1,
            service_id: 1,
            name: "c".to_owned(),
            check_type: check_type.to_owned(),
            url: String::new(),
            check_interval: 60,
            next_check_time: 0,
            processing_started_at: 0,
            status: "processing".to_owned(),
            disabled: false,
            data: PyJson::dict(),
            claim_started_at: 0,
        }
    }

    #[test]
    fn unknown_type_message_lists_the_registration_order() {
        assert_eq!(
            unknown_check_type_message("legacy", &REGISTERED_TYPES),
            "No executor registered for check type 'legacy'. Registered types: http, \
             json-http, dns, tcp, json-metrics, imap, smtp, ping"
        );
        assert_eq!(
            unknown_check_type_message("x", &[]),
            "No executor registered for check type 'x'. Registered types: none"
        );
    }

    #[test]
    fn every_registered_type_has_an_executor() {
        let registry = Registry::new();
        let batch = Arc::new(BatchCtx::new());
        for check_type in registry.registered_types() {
            assert!(registry.execute(check(check_type), batch.clone()).is_some());
        }
        assert!(registry.execute(check("HTTP"), batch.clone()).is_none());
        assert!(registry.execute(check("internal"), batch).is_none());
    }
}
