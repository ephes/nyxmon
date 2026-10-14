//! `notification.py:LoggingNotifier`, used without `--enable-telegram`.

use std::sync::Arc;

use nyxmon_core::model::{Check, CheckResult};
use nyxmon_core::pyjson::PyJson;

use crate::log::{Level, NotifyLog, TracingLog};
use crate::pystr::{py_repr, py_str};
use crate::{Delivery, Notifier, NotifyFuture};

/// Logs one ERROR line per alert under `nyxmon.adapters.notification`;
/// writing the line is the delivery.
///
/// In `--shadow` mode the line is machine-readable for the shadow
/// comparison: `notification check_id=<id> status=<s> error_type=<t>`
/// (`<t>` is `str(error_type)`, empty when absent). Otherwise it follows the
/// Python text (PLAN.md P2).
#[derive(Clone)]
pub struct LoggingNotifier {
    shadow: bool,
    log: Arc<dyn NotifyLog>,
}

impl LoggingNotifier {
    pub fn new(shadow: bool) -> Self {
        LoggingNotifier::with_log(shadow, Arc::new(TracingLog))
    }

    pub fn with_log(shadow: bool, log: Arc<dyn NotifyLog>) -> Self {
        LoggingNotifier { shadow, log }
    }

    /// The line logged for one alert.
    pub fn line(&self, check: &Check, result: &CheckResult) -> String {
        if self.shadow {
            let error_type = result
                .data
                .get("error_type")
                .map_or_else(String::new, py_str);
            return format!(
                "notification check_id={} status={} error_type={error_type}",
                check.check_id, result.status
            );
        }
        let check_name = if check.name.is_empty() {
            format!("Check {}", check.check_id)
        } else {
            check.name.clone()
        };
        format!(
            "Check failed: {check_name} (ID: {}), Result: Result(result_id=None, check_id={} \
             status={}, data={})",
            check.check_id,
            result.check_id,
            result.status,
            py_repr(&PyJson::Dict(result.data.clone()))
        )
    }
}

impl Notifier for LoggingNotifier {
    fn notify_check_failed<'a>(
        &'a self,
        check: &'a Check,
        result: &'a CheckResult,
    ) -> NotifyFuture<'a> {
        Box::pin(async move {
            self.log.log(Level::Error, &self.line(check, result));
            Ok(Delivery::Delivered)
        })
    }
}
