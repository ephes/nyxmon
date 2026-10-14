//! Counterparts of `tests/unit/test_runner_shutdown_stream.py`, the runner
//! parts of `test_shutdown_flush.py` and `test_executor_registry.py`.

use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use nyxmon_checks::registry::ExecFuture;
use nyxmon_core::config::PyException;
use nyxmon_core::model::NotificationState;
use nyxmon_store::{PersistRequest, Store, StoreError};

use super::*;
use crate::logging;
use crate::results::{ResultHandler, ResultStore};
use crate::testutil::{ManualClock, ScriptedNotifier, TestDb, check, handler};

type Behaviour = dyn Fn(Check) -> ExecFuture + Send + Sync;

/// Executors registered for `http` only, running `behaviour`.
struct FakeExecutors(Box<Behaviour>);

impl Executors for FakeExecutors {
    fn registered_types(&self) -> &[&'static str] {
        &["http", "dns"]
    }

    fn execute(&self, check: Check, _batch: Arc<BatchCtx>) -> Option<ExecFuture> {
        (check.check_type != "legacy").then(|| (self.0)(check))
    }
}

fn executors(
    behaviour: impl Fn(Check) -> ExecFuture + Send + Sync + 'static,
) -> Arc<dyn Executors> {
    Arc::new(FakeExecutors(Box::new(behaviour)))
}

fn ok(check_id: i64) -> CheckResult {
    CheckResult::new(check_id, ResultStatus::Ok, PyDict::new())
}

fn dumps(result: &CheckResult) -> String {
    PyJson::Dict(result.data.clone()).to_json_string().unwrap()
}

type Received = Arc<Mutex<Vec<(i64, CheckResult)>>>;

fn recorder() -> (
    Received,
    impl FnMut(Check, CheckResult) -> std::future::Ready<()>,
) {
    let received: Received = Arc::default();
    let sink = received.clone();
    (received, move |check: Check, result| {
        sink.lock().unwrap().push((check.check_id, result));
        std::future::ready(())
    })
}

#[tokio::test]
async fn runner_results_name_the_registered_types_and_the_exception() {
    let executors = executors(|check| {
        Box::pin(async move {
            match check.check_id {
                2 => Err(PyException::TYPE_ERROR),
                3 => panic!("executor bug"),
                _ => Ok(ok(check.check_id)),
            }
        })
    });
    let mut legacy = check(1);
    legacy.check_type = "legacy".to_owned();
    let mut undecodable = check(4);
    undecodable.data = PyJson::Null;
    let checks = vec![legacy, check(2), check(3), undecodable];
    let (received, receive) = recorder();
    let (capture, _guard) = logging::capture();
    run_batch(&executors, checks, None, receive).await;

    let mut received = received.lock().unwrap().clone();
    received.sort_by_key(|(id, _)| *id);
    let rendered: Vec<(i64, &str, String)> = received
        .iter()
        .map(|(id, result)| (*id, result.status.as_str(), dumps(result)))
        .collect();
    assert_eq!(
        rendered,
        [
            (
                1,
                "error",
                r#"{"error_type": "unknown_check_type", "error_msg": "No executor registered for check type 'legacy'. Registered types: http, dns", "check_type": "legacy"}"#
                    .to_owned()
            ),
            (
                2,
                "error",
                r#"{"error_type": "executor_exception", "error_msg": "TypeError", "check_type": "http"}"#
                    .to_owned()
            ),
            (
                3,
                "error",
                r#"{"error_type": "executor_exception", "error_msg": "panic", "check_type": "http"}"#
                    .to_owned()
            ),
            (
                4,
                "error",
                r#"{"error_type": "configuration_error", "error_msg": "check data is not valid JSON"}"#
                    .to_owned()
            ),
        ]
    );
    let mut lines = capture.messages();
    lines.sort();
    assert_eq!(
        lines,
        [
            "check executor raised unexpectedly for check_id=2 check_type=http\nTypeError",
            "check executor raised unexpectedly for check_id=3 check_type=http\npanic",
        ]
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn results_are_handed_over_one_at_a_time_in_completion_order() {
    let release_slow = Arc::new(Flag::new());
    let executors = executors({
        let release_slow = release_slow.clone();
        move |check| {
            let release_slow = release_slow.clone();
            Box::pin(async move {
                if check.check_id == 1 {
                    release_slow.wait().await;
                }
                Ok(ok(check.check_id))
            })
        }
    });
    let order = Arc::new(Mutex::new(Vec::new()));
    let active = Arc::new(AtomicUsize::new(0));
    let receive = {
        let order = order.clone();
        let active = active.clone();
        let release_slow = release_slow.clone();
        move |check: Check, _result: CheckResult| {
            let order = order.clone();
            let active = active.clone();
            let release_slow = release_slow.clone();
            async move {
                assert_eq!(
                    active.fetch_add(1, Ordering::SeqCst),
                    0,
                    "handled concurrently"
                );
                order.lock().unwrap().push(check.check_id);
                // The fast result is handled while the slow check still runs.
                release_slow.set();
                tokio::time::sleep(Duration::from_millis(20)).await;
                active.fetch_sub(1, Ordering::SeqCst);
            }
        }
    };
    tokio::time::timeout(
        Duration::from_secs(5),
        run_batch(
            &executors,
            vec![check(1), check(2), check(3)],
            None,
            receive,
        ),
    )
    .await
    .unwrap();
    let order = order.lock().unwrap().clone();
    assert_eq!(order.len(), 3);
    assert_eq!(order.last(), Some(&1));
}

/// Persisting the result of check 1 fails.
struct FailingFirst(Store);

impl ResultStore for FailingFirst {
    async fn get_notification_state(&self, check_id: i64) -> Result<NotificationState, StoreError> {
        self.0.get_notification_state(check_id).await
    }

    async fn persist_check_result(&self, request: PersistRequest) -> Result<bool, StoreError> {
        if request.check.check_id == 1 {
            return Err(StoreError::Json("injected persistence failure".to_owned()));
        }
        self.0.persist_check_result(request).await
    }

    async fn acknowledge_notification_attempt(
        &self,
        id: i64,
        seq: i64,
    ) -> Result<bool, StoreError> {
        self.0.acknowledge_notification_attempt(id, seq).await
    }
}

#[tokio::test]
async fn a_result_handler_failure_does_not_abort_a_waiting_sibling() {
    let db = TestDb::new();
    let mut claims = Vec::new();
    for check_id in [1, 2] {
        let mut claim = check(check_id);
        claim.status = "processing".to_owned();
        claim.processing_started_at = 100;
        claim.claim_started_at = 100;
        db.insert(&claim);
        claims.push(claim);
    }
    let clock = ManualClock::new(1_000);
    let handler: Arc<ResultHandler<FailingFirst>> = Arc::new(handler(
        FailingFirst(db.store()),
        ScriptedNotifier::new(None),
        &[],
        &clock,
    ));
    let first_handled = Arc::new(Flag::new());
    let executors = executors({
        let first_handled = first_handled.clone();
        move |check| {
            let first_handled = first_handled.clone();
            Box::pin(async move {
                if check.check_id == 2 {
                    first_handled.wait().await;
                }
                Ok(ok(check.check_id))
            })
        }
    });
    let receive = {
        let handler = handler.clone();
        let first_handled = first_handled.clone();
        move |check: Check, result: CheckResult| {
            let handler = handler.clone();
            let first_handled = first_handled.clone();
            async move {
                let check_id = check.check_id;
                handler.receive(check, result).await;
                if check_id == 1 {
                    first_handled.set();
                }
            }
        }
    };
    let (capture, _guard) = logging::capture();
    tokio::time::timeout(
        Duration::from_secs(5),
        run_batch(&executors, claims, None, receive),
    )
    .await
    .unwrap();
    assert_eq!(db.results(), [(2, "ok".to_owned(), "{}".to_owned())]);
    assert_eq!(db.schedule(2), ("idle".to_owned(), 1_300, 0));
    assert_eq!(
        capture.messages(),
        ["result handling failed for check_id=1\ninjected persistence failure"]
    );
}

/// Sets the flag when dropped (a check task being cancelled).
struct SetOnDrop(Arc<Flag>);

impl Drop for SetOnDrop {
    fn drop(&mut self) {
        self.0.set();
    }
}

#[tokio::test]
async fn an_intake_stop_hands_over_finished_results_and_cancels_the_rest() {
    let stop = Arc::new(IntakeStop::default());
    let first_receiving = Arc::new(Flag::new());
    let second_sent = Arc::new(Flag::new());
    let unfinished_cancelled = Arc::new(Flag::new());
    let executors = executors({
        let first_receiving = first_receiving.clone();
        let second_sent = second_sent.clone();
        let unfinished_cancelled = unfinished_cancelled.clone();
        move |check| {
            let first_receiving = first_receiving.clone();
            let second_sent = second_sent.clone();
            let guard = SetOnDrop(unfinished_cancelled.clone());
            Box::pin(async move {
                match check.check_id {
                    1 => {
                        std::mem::forget(guard);
                    }
                    2 => {
                        std::mem::forget(guard);
                        first_receiving.wait().await;
                        // Finished while the first result is being handled.
                        tokio::spawn({
                            let second_sent = second_sent.clone();
                            async move { second_sent.set() }
                        });
                    }
                    _ => {
                        let _guard = guard;
                        std::future::pending::<()>().await;
                    }
                }
                Ok(ok(check.check_id))
            })
        }
    });
    let received = Arc::new(Mutex::new(Vec::new()));
    let receive = {
        let received = received.clone();
        let stop = stop.clone();
        move |check: Check, _result: CheckResult| {
            let received = received.clone();
            let stop = stop.clone();
            let first_receiving = first_receiving.clone();
            let second_sent = second_sent.clone();
            async move {
                received.lock().unwrap().push(check.check_id);
                if check.check_id == 1 {
                    // A slow result handler (an alert) while check 2 finishes.
                    first_receiving.set();
                    second_sent.wait().await;
                    tokio::time::sleep(Duration::from_millis(20)).await;
                    stop.requested.set();
                    tokio::time::sleep(Duration::from_millis(100)).await;
                }
            }
        }
    };
    tokio::time::timeout(
        Duration::from_secs(5),
        run_batch(
            &executors,
            vec![check(1), check(2), check(3)],
            Some(&stop),
            receive,
        ),
    )
    .await
    .unwrap();
    assert_eq!(*received.lock().unwrap(), [1, 2]);
    assert!(unfinished_cancelled.is_set());
    assert!(stop.drained.is_set());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_intake_stop_reports_drained_while_a_check_cannot_be_cancelled() {
    let stop = Arc::new(IntakeStop::default());
    let release = Arc::new(std::sync::Barrier::new(2));
    let executors = executors({
        let release = release.clone();
        move |check| {
            let release = release.clone();
            Box::pin(async move {
                // A blocking section that ignores cancellation.
                tokio::task::spawn_blocking(move || {
                    release.wait();
                })
                .await
                .unwrap();
                Ok(ok(check.check_id))
            })
        }
    });
    let batch = tokio::spawn({
        let stop = stop.clone();
        async move {
            run_batch(&executors, vec![check(1)], Some(&stop), |_, _| async {
                panic!("a result after the intake stop is not handed over")
            })
            .await;
        }
    });
    tokio::time::sleep(Duration::from_millis(20)).await;
    stop.requested.set();
    tokio::time::timeout(Duration::from_secs(5), stop.drained.wait())
        .await
        .unwrap();
    release.wait();
    batch.await.unwrap();
}

#[tokio::test]
async fn an_intake_stop_after_every_result_still_reports_drained() {
    let stop = IntakeStop::default();
    let executors = executors(|check| Box::pin(async move { Ok(ok(check.check_id)) }));
    let (received, receive) = recorder();
    stop.requested.set();
    run_batch(&executors, vec![check(1)], Some(&stop), receive).await;
    assert!(stop.drained.is_set());
    // Requested before anything finished: nothing is handed over.
    assert!(received.lock().unwrap().is_empty());
}

#[tokio::test]
async fn dropping_the_batch_cancels_the_running_checks() {
    let cancelled = Arc::new(Flag::new());
    let started = Arc::new(Flag::new());
    let executors = executors({
        let cancelled = cancelled.clone();
        let started = started.clone();
        move |check| {
            let guard = SetOnDrop(cancelled.clone());
            let started = started.clone();
            Box::pin(async move {
                let _guard = guard;
                started.set();
                std::future::pending::<()>().await;
                Ok(ok(check.check_id))
            })
        }
    });
    let batch = tokio::spawn(async move {
        run_batch(&executors, vec![check(1)], None, |_, _| async {}).await;
    });
    started.wait().await;
    batch.abort();
    assert!(batch.await.unwrap_err().is_cancelled());
    tokio::time::timeout(Duration::from_secs(5), cancelled.wait())
        .await
        .unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_result_finishing_after_the_cutoff_is_not_handed_over() {
    // Check 2 is inside a poll that cannot be interrupted when the intake
    // stop arrives and finishes 100 ms later; abort cannot stop it.
    let stop = Arc::new(IntakeStop::default());
    let first_receiving = Arc::new(Flag::new());
    let executors = executors({
        let stop = stop.clone();
        let first_receiving = first_receiving.clone();
        move |check| {
            let stop = stop.clone();
            let first_receiving = first_receiving.clone();
            Box::pin(async move {
                if check.check_id == 2 {
                    first_receiving.wait().await;
                    while !stop.requested.is_set() {
                        std::thread::sleep(Duration::from_millis(1));
                    }
                    std::thread::sleep(Duration::from_millis(100));
                }
                Ok(ok(check.check_id))
            })
        }
    });
    let received = Arc::new(Mutex::new(Vec::new()));
    let receive = {
        let received = received.clone();
        let stop = stop.clone();
        move |check: Check, _result: CheckResult| {
            let received = received.clone();
            let stop = stop.clone();
            let first_receiving = first_receiving.clone();
            async move {
                received.lock().unwrap().push(check.check_id);
                if check.check_id == 1 {
                    first_receiving.set();
                    tokio::time::sleep(Duration::from_millis(20)).await;
                    stop.requested.set();
                    // Still handling while check 2 finishes after the cutoff.
                    tokio::time::sleep(Duration::from_millis(300)).await;
                }
            }
        }
    };
    tokio::time::timeout(
        Duration::from_secs(5),
        run_batch(&executors, vec![check(1), check(2)], Some(&stop), receive),
    )
    .await
    .unwrap();
    assert_eq!(*received.lock().unwrap(), [1]);
    assert!(stop.drained.is_set());
}
