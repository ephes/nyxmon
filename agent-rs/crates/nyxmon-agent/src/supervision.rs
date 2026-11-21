//! Own the monitoring tasks and classify unexpected completion without payloads.
use std::future::{Future, pending, ready};
use std::pin::pin;
use std::time::Duration;
use tokio::task::{JoinError, JoinHandle};
use tokio::time::{Instant, timeout_at};

async fn optional_join(task: &mut Option<JoinHandle<()>>) -> Result<(), JoinError> {
    match task {
        Some(task) => task.await,
        None => pending().await,
    }
}

fn category(result: &Result<(), JoinError>) -> &'static str {
    match result {
        Ok(()) => "returned",
        Err(err) if err.is_panic() => "panicked",
        Err(_) => "cancelled",
    }
}

fn report(task: &str, category: &str) {
    tracing::error!(target: "nyxmon.entrypoints.cli", "monitoring task {task} unexpectedly {category}");
}

async fn auxiliary_failed(name: &str, task: Option<JoinHandle<()>>, deadline: Instant) -> bool {
    let Some(task) = task else {
        return false;
    };
    match timeout_at(deadline, task).await {
        Ok(Ok(())) => false,
        Ok(Err(err)) if err.is_cancelled() => false,
        Ok(result) => {
            report(name, category(&result));
            true
        }
        Err(_) => {
            report(name, "did not stop");
            true
        }
    }
}

type MonitoringTasks = (
    JoinHandle<()>,
    Option<JoinHandle<()>>,
    Option<JoinHandle<()>>,
);

/// Check for a buffered stop signal before starting tasks, then supervise them.
/// The collector gets 60s from the stop request (max 30s grace + 15s flush + DB cleanup allowance).
/// Observer/cleaner cancellation is intentional only after shutdown starts.
pub(crate) async fn supervise(
    start: impl FnOnce() -> MonitoringTasks,
    shutdown: impl Future<Output = ()>,
    request_stop: impl FnOnce(),
) -> bool {
    // Keep the same signal future for both the startup check and supervision.
    let mut shutdown = pin!(shutdown);
    tokio::select! {
        biased;
        () = &mut shutdown => return false,
        () = ready(()) => {},
    }
    let (mut collector, mut observer, mut cleaner) = start();
    let mut failed = false;
    let mut collector_done = false;
    tokio::select! {
        biased;
        () = shutdown => {},
        result = &mut collector => {
            report("collector", category(&result));
            collector_done = true;
            failed = true;
        },
        result = optional_join(&mut observer) => {
            report("observer", category(&result));
            observer = None;
            failed = true;
        },
        result = optional_join(&mut cleaner) => {
            report("cleaner", category(&result));
            cleaner = None;
            failed = true;
        },
    }
    let collector_deadline = Instant::now() + Duration::from_secs(60);
    request_stop();
    // Request cancellation of both tasks before waiting on either.
    let auxiliary_deadline = Instant::now() + Duration::from_secs(1);
    for task in [&observer, &cleaner].into_iter().flatten() {
        task.abort();
    }
    let (observer_failed, cleaner_failed) = tokio::join!(
        auxiliary_failed("observer", observer, auxiliary_deadline),
        auxiliary_failed("cleaner", cleaner, auxiliary_deadline),
    );
    failed |= observer_failed || cleaner_failed;
    if !collector_done {
        match timeout_at(collector_deadline, &mut collector).await {
            Ok(Ok(())) => {}
            Ok(result) => {
                report("collector", category(&result));
                failed = true;
            }
            Err(_) => {
                report("collector", "exceeded shutdown deadline");
                failed = true;
                collector.abort();
                let _ = tokio::time::timeout(Duration::from_secs(1), collector).await;
            }
        }
    }
    failed
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sync::Flag;
    use std::sync::Arc;

    // Run global hook/subscriber changes in isolated child processes only.
    #[test]
    fn panic_payloads_never_reach_real_stderr() {
        for role in ["executor", "observer", "cleaner", "main"] {
            let output = std::process::Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "supervision::tests::panic_stderr_child",
                    "--nocapture",
                ])
                .env("NYXMON_TEST_PANIC_ROLE", role)
                .env("RUST_BACKTRACE", "1")
                .output()
                .unwrap();
            let stderr = String::from_utf8(output.stderr).unwrap();
            assert_eq!(
                output.status.success(),
                role == "executor",
                "{role}: {stderr}"
            );
            assert!(
                stderr.contains("nyxmon-agent: panic (payload withheld)"),
                "{role}: {stderr}"
            );
            assert!(
                !stderr.contains("invented-private-panic-value"),
                "{role}: {stderr}"
            );
            assert!(!stderr.contains("stack backtrace"), "{role}: {stderr}");
            if role == "executor" {
                assert!(stderr.contains("check executor raised unexpectedly"));
                assert!(stderr.contains("check_id=1"));
            } else if role != "main" {
                assert!(stderr.contains(&format!("monitoring task {role} unexpectedly panicked")));
            }
        }
    }

    #[test]
    fn panic_stderr_child() {
        let Ok(role) = std::env::var("NYXMON_TEST_PANIC_ROLE") else {
            return;
        };
        let _logging_shutdown = crate::logging::LoggingShutdownGuard;
        assert!(crate::logging::install_logging("INFO"));
        if role == "main" {
            panic!("invented-private-panic-value");
        }
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let failed = runtime.block_on(async {
            if role == "executor" {
                use nyxmon_checks::{
                    ctx::BatchCtx,
                    registry::{ExecFuture, Executors},
                };
                use nyxmon_core::model::{Check, ResultStatus};
                struct PanickingExecutor;
                impl Executors for PanickingExecutor {
                    fn registered_types(&self) -> &[&'static str] {
                        &["http"]
                    }
                    fn execute(&self, _: Check, _: Arc<BatchCtx>) -> Option<ExecFuture> {
                        Some(Box::pin(async { panic!("invented-private-panic-value") }))
                    }
                }
                // The same runner used by run-check: an executor panic is an
                // error result, not an agent-wide failure.
                let result = crate::run_check::execute(
                    Arc::new(PanickingExecutor),
                    crate::testutil::check(1),
                )
                .await;
                assert_eq!(result.status, ResultStatus::Error);
                assert_eq!(
                    result.data.get("error_msg"),
                    Some(&nyxmon_core::PyJson::from("panic"))
                );
                return false;
            }
            let stop = Arc::new(Flag::new());
            let stopped = stop.clone();
            let collector = tokio::spawn(async move {
                stopped.wait().await;
            });
            let observer_role = role.clone();
            let observer = tokio::spawn(async move {
                if observer_role == "observer" {
                    panic!("invented-private-panic-value");
                }
                pending::<()>().await;
            });
            let cleaner = tokio::spawn(async move {
                if role == "cleaner" {
                    panic!("invented-private-panic-value");
                }
                pending::<()>().await;
            });
            supervise(
                || (collector, Some(observer), Some(cleaner)),
                pending(),
                || stop.set(),
            )
            .await
        });
        crate::logging::shutdown_logging();
        std::process::exit(i32::from(failed));
    }

    #[tokio::test(start_paused = true)]
    async fn unexpected_task_exits_fail_and_stop_the_collector() {
        for which in 0..3 {
            let stop = Arc::new(Flag::new());
            let stopped = stop.clone();
            let collector = tokio::spawn(async move {
                if which == 0 {
                    panic!("panic-sentinel-secret");
                }
                stopped.wait().await;
            });
            let observer = tokio::spawn(async move {
                if which == 1 {
                    return;
                }
                pending::<()>().await;
            });
            let cleaner = tokio::spawn(async move {
                if which == 2 {
                    return;
                }
                pending::<()>().await;
            });
            let (capture, _guard) = crate::logging::capture();
            assert!(
                supervise(
                    || (collector, Some(observer), Some(cleaner)),
                    pending(),
                    || stop.set()
                )
                .await
            );
            let logs = capture.messages().join("\n");
            assert!(logs.contains(["collector", "observer", "cleaner"][which]));
            assert!(!logs.contains("panic-sentinel-secret"));
            assert!(stop.is_set());
        }
    }

    #[tokio::test]
    async fn pending_startup_signal_prevents_first_claim() {
        use std::sync::atomic::{AtomicUsize, Ordering};

        let claims = Arc::new(AtomicUsize::new(0));
        let collector_claims = claims.clone();
        let (stop_tx, stop_rx) = tokio::sync::oneshot::channel();
        stop_tx.send(()).unwrap();
        let failed = supervise(
            || {
                let collector = tokio::spawn(async move {
                    collector_claims.fetch_add(1, Ordering::Relaxed);
                });
                (collector, None, None)
            },
            async { stop_rx.await.unwrap() },
            || {},
        )
        .await;

        assert_eq!(claims.load(Ordering::Relaxed), 0);
        assert!(!failed);
    }

    #[tokio::test(start_paused = true)]
    async fn intentional_shutdown_and_auxiliary_aborts_succeed() {
        let stop = Arc::new(Flag::new());
        let stopped = stop.clone();
        let collector = tokio::spawn(async move {
            stopped.wait().await;
        });
        assert!(
            !supervise(
                || (
                    collector,
                    Some(tokio::spawn(pending())),
                    Some(tokio::spawn(pending()))
                ),
                tokio::task::yield_now(),
                || stop.set()
            )
            .await
        );
    }

    #[tokio::test(start_paused = true)]
    async fn observer_finishing_inside_the_stop_callback_is_successful() {
        let stop = Arc::new(Flag::new());
        let stopped = stop.clone();
        let collector = tokio::spawn(async move {
            stopped.wait().await;
        });
        let observer = tokio::spawn(async {});
        while !observer.is_finished() {
            tokio::task::yield_now().await;
        }
        assert!(
            !supervise(
                || (collector, Some(observer), None),
                tokio::task::yield_now(),
                || stop.set()
            )
            .await
        );
    }

    // Keep simulated non-cooperative work off the test runtime so its
    // paused clock can advance independently.
    fn stalled_task(
        runtime: &tokio::runtime::Runtime,
    ) -> (JoinHandle<()>, std::sync::mpsc::Sender<()>) {
        let (started, wait_for_start) = std::sync::mpsc::channel();
        let (release, wait_for_release) = std::sync::mpsc::channel();
        let task = runtime.spawn_blocking(move || {
            started.send(()).unwrap();
            let _ = wait_for_release.recv();
        });
        wait_for_start.recv().unwrap();
        (task, release)
    }

    #[test]
    fn cleaner_is_aborted_while_observer_is_still_draining() {
        let blocking = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let (observer, _release_observer) = stalled_task(&blocking);
        tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .start_paused(true)
            .build()
            .unwrap()
            .block_on(async {
                let (started, wait_for_start) = tokio::sync::oneshot::channel();
                let (held_by_cleaner, cleaner_dropped) = tokio::sync::oneshot::channel::<()>();
                let start = tokio::time::Instant::now();
                let stop = Arc::new(Flag::new());
                let collector_stop = stop.clone();
                let supervisor = supervise(
                    || {
                        let cleaner = tokio::spawn(async move {
                            started.send(()).unwrap();
                            pending::<()>().await;
                            drop(held_by_cleaner);
                        });
                        (
                            tokio::spawn(async move { collector_stop.wait().await }),
                            Some(observer),
                            Some(cleaner),
                        )
                    },
                    async { wait_for_start.await.unwrap() },
                    || stop.set(),
                );
                tokio::pin!(supervisor);
                tokio::select! {
                    biased;
                    result = cleaner_dropped => assert!(result.is_err()),
                    _ = &mut supervisor => panic!("cleaner was still live when shutdown returned"),
                }
                assert_eq!(
                    start.elapsed(),
                    Duration::ZERO,
                    "cleaner abort waited for observer"
                );
                assert!(supervisor.await);
            });
    }

    #[test]
    fn stalled_auxiliaries_share_one_deadline_and_report_both_tasks() {
        let blocking = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let (observer, _release_observer) = stalled_task(&blocking);
        let (cleaner, _release_cleaner) = stalled_task(&blocking);
        tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .start_paused(true)
            .build()
            .unwrap()
            .block_on(async {
                let (capture, _guard) = crate::logging::capture();
                let start = tokio::time::Instant::now();
                let stop = Arc::new(Flag::new());
                let collector_stop = stop.clone();
                assert!(
                    supervise(
                        || (
                            tokio::spawn(async move { collector_stop.wait().await }),
                            Some(observer),
                            Some(cleaner),
                        ),
                        tokio::task::yield_now(),
                        || stop.set(),
                    )
                    .await
                );
                assert_eq!(start.elapsed(), Duration::from_secs(1));
                assert!(capture.contains("monitoring task observer unexpectedly did not stop"));
                assert!(capture.contains("monitoring task cleaner unexpectedly did not stop"));
            });
    }

    #[test]
    fn collector_deadline_is_independent_of_auxiliary_drain() {
        let blocking = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let (observer, _release_observer) = stalled_task(&blocking);
        tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .start_paused(true)
            .build()
            .unwrap()
            .block_on(async {
                for observer in [None, Some(observer)] {
                    let mut stop_requested_at = None;
                    assert!(
                        supervise(
                            || (tokio::spawn(pending()), observer, None),
                            tokio::task::yield_now(),
                            || stop_requested_at = Some(Instant::now()),
                        )
                        .await
                    );
                    assert_eq!(
                        stop_requested_at.unwrap().elapsed(),
                        Duration::from_secs(60)
                    );
                }
            });
    }

    #[tokio::test(start_paused = true)]
    async fn panic_during_drain_and_wedged_collector_fail() {
        let stop = Arc::new(Flag::new());
        let stopped = stop.clone();
        let collector = tokio::spawn(async move {
            stopped.wait().await;
            panic!("secret");
        });
        assert!(
            supervise(
                || (collector, None, None),
                tokio::task::yield_now(),
                || stop.set()
            )
            .await
        );
        let start = tokio::time::Instant::now();
        assert!(
            supervise(
                || (tokio::spawn(pending()), None, None),
                tokio::task::yield_now(),
                || {}
            )
            .await
        );
        assert_eq!(start.elapsed(), Duration::from_secs(60));
    }
}
