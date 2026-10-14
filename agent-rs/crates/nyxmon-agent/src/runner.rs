//! The check runner (`adapters/runner/async_runner.py`, PLAN.md 3.7, 5.9).
//!
//! Every check of a batch starts at once; finished results are handed to
//! the result handler one at a time, in completion order, while the other
//! checks keep running. A shutdown intake stop cancels the checks still
//! running, hands over the results that finished before it, and reports
//! `drained` (`IntakeStop`, PLAN.md 5.8).

use std::collections::HashMap;
use std::pin::pin;
use std::sync::Arc;

use nyxmon_checks::ctx::BatchCtx;
use nyxmon_checks::registry::{Executors, unknown_check_type_message};
use nyxmon_core::model::{Check, CheckResult, ResultStatus};
use nyxmon_core::pyjson::{PyDict, PyJson};
use nyxmon_store::data_is_undecodable;
use tokio::task::{JoinError, JoinSet};

use crate::sync::{Flag, wait_for};

/// `domain/commands.py:IntakeStop`.
#[derive(Debug, Default)]
pub struct IntakeStop {
    /// Set by shutdown when the grace period ends.
    pub requested: Flag,
    /// Set by the runner once every result that finished before the
    /// request was handed over.
    pub drained: Flag,
}

fn error_result(check: &Check, error_type: &str, error_msg: String) -> CheckResult {
    let mut data = PyDict::new();
    data.insert("error_type".to_owned(), PyJson::from(error_type));
    data.insert("error_msg".to_owned(), PyJson::from(error_msg));
    data.insert(
        "check_type".to_owned(),
        PyJson::from(check.check_type.as_str()),
    );
    CheckResult::new(check.check_id, ResultStatus::Error, data)
}

/// The result of a check whose type has no executor (PLAN.md 1.6).
pub fn unknown_check_type_result(check: &Check, registered: &[&str]) -> CheckResult {
    error_result(
        check,
        "unknown_check_type",
        unknown_check_type_message(&check.check_type, registered),
    )
}

/// The result of an exception or panic escaping an executor (PLAN.md 1.6,
/// D-4: a panic reads `panic`).
pub fn executor_exception_result(check: &Check, class: &str) -> CheckResult {
    error_result(check, "executor_exception", class.to_owned())
}

/// The result of a check whose `data` column cannot be decoded (PLAN.md
/// D-8; Python raises while loading the claimed row).
pub fn undecodable_data_result(check: &Check) -> CheckResult {
    let mut data = PyDict::new();
    data.insert("error_type".to_owned(), PyJson::from("configuration_error"));
    data.insert(
        "error_msg".to_owned(),
        PyJson::from("check data is not valid JSON"),
    );
    CheckResult::new(check.check_id, ResultStatus::Error, data)
}

fn log_executor_exception(check: &Check, class: &str) {
    tracing::error!(
        target: "nyxmon.adapters.runner.async_runner",
        "check executor raised unexpectedly for check_id={} check_type={}\n{class}",
        check.check_id,
        check.check_type
    );
}

/// `_run_one`.
async fn run_one(executors: Arc<dyn Executors>, check: Check, batch: Arc<BatchCtx>) -> CheckResult {
    let registered = executors.registered_types();
    if registered.contains(&check.check_type.as_str()) && data_is_undecodable(&check.data) {
        return undecodable_data_result(&check);
    }
    let Some(execution) = executors.execute(check.clone(), batch) else {
        return unknown_check_type_result(&check, registered);
    };
    match execution.await {
        Ok(result) => result,
        Err(exception) => {
            log_executor_exception(&check, exception.0);
            executor_exception_result(&check, exception.0)
        }
    }
}

/// Run one batch (`_async_run_batch`) and hand every result to `receive`
/// with its claimed check, one at a time.
///
/// Dropping the returned future aborts the checks still running (PLAN.md
/// D-3).
pub async fn run_batch<F, Fut>(
    executors: &Arc<dyn Executors>,
    checks: Vec<Check>,
    intake_stop: Option<&IntakeStop>,
    mut receive: F,
) where
    F: FnMut(Check, CheckResult) -> Fut,
    Fut: Future<Output = ()>,
{
    // One shared HTTP session per batch (`httpx.AsyncClient` of the batch).
    let batch = Arc::new(BatchCtx::new());
    let mut running = JoinSet::new();
    let mut claimed: HashMap<tokio::task::Id, Check> = HashMap::new();
    for check in checks {
        let handle = running.spawn(run_one(executors.clone(), check.clone(), batch.clone()));
        claimed.insert(handle.id(), check);
    }
    let requested = intake_stop.map(|stop| &stop.requested);
    let mut stopped = false;
    loop {
        let joined = if stopped {
            // Only results that already finished are handed over.
            match running.try_join_next_with_id() {
                Some(joined) => joined,
                None => break,
            }
        } else {
            tokio::select! {
                biased;
                () = wait_for(requested) => {
                    running.abort_all();
                    stopped = true;
                    continue;
                }
                joined = running.join_next_with_id() => match joined {
                    Some(joined) => joined,
                    None => break,
                },
            }
        };
        let Some((check, result)) = finished(&mut claimed, joined) else {
            continue;
        };
        let handled = receive(check, result);
        if stopped {
            handled.await;
            continue;
        }
        // A slow result handler (an alert being sent) does not delay the
        // intake stop of the checks still running.
        let mut handled = pin!(handled);
        tokio::select! {
            biased;
            () = &mut handled => {}
            () = wait_for(requested) => {
                running.abort_all();
                stopped = true;
                handled.await;
            }
        }
    }
    if let Some(stop) = intake_stop
        && stop.requested.is_set()
    {
        stop.drained.set();
    }
}

/// The claimed check and result of a finished task; `None` for a task the
/// intake stop cancelled.
fn finished(
    claimed: &mut HashMap<tokio::task::Id, Check>,
    joined: Result<(tokio::task::Id, CheckResult), JoinError>,
) -> Option<(Check, CheckResult)> {
    match joined {
        Ok((id, result)) => claimed.remove(&id).map(|check| (check, result)),
        Err(err) if err.is_panic() => {
            let check = claimed.remove(&err.id())?;
            log_executor_exception(&check, "panic");
            let result = executor_exception_result(&check, "panic");
            Some((check, result))
        }
        Err(_) => None,
    }
}

#[cfg(test)]
mod tests;
