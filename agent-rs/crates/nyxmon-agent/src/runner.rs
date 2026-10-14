//! The check runner (`adapters/runner/async_runner.py`, PLAN.md 3.7, 5.9).
//!
//! Every check of a batch starts at once; finished results are handed to
//! the result handler one at a time, in completion order, while the other
//! checks keep running. A shutdown intake stop cancels the checks still
//! running, hands over the results that finished before it, and reports
//! `drained` (`IntakeStop`, PLAN.md 5.8).

use std::collections::HashMap;
use std::panic::AssertUnwindSafe;
use std::pin::{Pin, pin};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::task::{Context, Poll};

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

/// Turns a panic while polling the inner future into `Err(())`, so a
/// panicking executor still yields a result inside its task.
struct CatchUnwind<F>(Pin<Box<F>>);

impl<F: Future> Future for CatchUnwind<F> {
    type Output = Result<F::Output, ()>;

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        let inner = self.0.as_mut();
        match std::panic::catch_unwind(AssertUnwindSafe(|| inner.poll(cx))) {
            Ok(Poll::Ready(output)) => Poll::Ready(Ok(output)),
            Ok(Poll::Pending) => Poll::Pending,
            Err(_) => Poll::Ready(Err(())),
        }
    }
}

/// One check task: its result, or `None` when it finished after the intake
/// stop's cutoff. `abort_all` cannot interrupt a poll that is already
/// running, so the cutoff is checked by the task itself as it finishes.
async fn run_gated(
    executors: Arc<dyn Executors>,
    check: Check,
    batch: Arc<BatchCtx>,
    cutoff: Arc<AtomicBool>,
) -> Option<CheckResult> {
    let result = match CatchUnwind(Box::pin(run_one(executors, check.clone(), batch))).await {
        Ok(result) => result,
        Err(()) => {
            log_executor_exception(&check, "panic");
            executor_exception_result(&check, "panic")
        }
    };
    (!cutoff.load(Ordering::SeqCst)).then_some(result)
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
    let cutoff = Arc::new(AtomicBool::new(false));
    let mut running = JoinSet::new();
    let mut claimed: HashMap<tokio::task::Id, Check> = HashMap::new();
    for check in checks {
        let handle = running.spawn(run_gated(
            executors.clone(),
            check.clone(),
            batch.clone(),
            cutoff.clone(),
        ));
        claimed.insert(handle.id(), check);
    }
    // The intake stop: results finishing from now on are discarded.
    let stop_intake = |running: &mut JoinSet<Option<CheckResult>>| {
        cutoff.store(true, Ordering::SeqCst);
        running.abort_all();
    };
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
                    stop_intake(&mut running);
                    stopped = true;
                    continue;
                }
                joined = running.join_next_with_id() => match joined {
                    Some(joined) => joined,
                    None => break,
                },
            }
        };
        let Some((check, result)) = finished(&mut claimed, joined, stopped) else {
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
                stop_intake(&mut running);
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
/// intake stop cancelled or that finished after its cutoff.
fn finished(
    claimed: &mut HashMap<tokio::task::Id, Check>,
    joined: Result<(tokio::task::Id, Option<CheckResult>), JoinError>,
    stopped: bool,
) -> Option<(Check, CheckResult)> {
    match joined {
        Ok((id, result)) => {
            let check = claimed.remove(&id)?;
            result.map(|result| (check, result))
        }
        // Executor panics are caught inside the task; this is a panic of the
        // task itself, which cannot be placed relative to the cutoff.
        Err(err) if err.is_panic() && !stopped => {
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
