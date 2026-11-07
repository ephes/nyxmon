//! Own the monitoring tasks and classify unexpected completion without payloads.
use std::future::{Future, pending};
use std::time::Duration;
use tokio::task::{JoinError, JoinHandle};

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

/// Wait for a stop signal or a required task, then stop and reap owned tasks.
/// The collector gets 60s (max 30s grace + 15s flush + DB cleanup allowance).
/// Observer/cleaner cancellation is intentional only after shutdown starts.
pub(crate) async fn supervise(
    mut collector: JoinHandle<()>,
    mut observer: Option<JoinHandle<()>>,
    mut cleaner: Option<JoinHandle<()>>,
    shutdown: impl Future<Output = ()>,
    request_stop: impl FnOnce(),
) -> bool {
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
    request_stop();
    // Once shutdown starts, auxiliary normal completion and abort are intentional.
    for (name, slot) in [("observer", &mut observer), ("cleaner", &mut cleaner)] {
        if let Some(task) = slot.take() {
            task.abort();
            match tokio::time::timeout(Duration::from_secs(1), task).await {
                Ok(Ok(())) => {}
                Ok(Err(err)) if err.is_cancelled() => {}
                Ok(result) => {
                    report(name, category(&result));
                    failed = true;
                }
                Err(_) => {
                    report(name, "did not stop");
                    failed = true;
                }
            }
        }
    }
    if !collector_done {
        match tokio::time::timeout(Duration::from_secs(60), &mut collector).await {
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
            supervise(collector, Some(observer), Some(cleaner), pending(), || {
                stop.set()
            })
            .await
        });
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
                supervise(collector, Some(observer), Some(cleaner), pending(), || stop
                    .set())
                .await
            );
            let logs = capture.messages().join("\n");
            assert!(logs.contains(["collector", "observer", "cleaner"][which]));
            assert!(!logs.contains("panic-sentinel-secret"));
            assert!(stop.is_set());
        }
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
                collector,
                Some(tokio::spawn(pending())),
                Some(tokio::spawn(pending())),
                async {},
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
        assert!(!supervise(collector, Some(observer), None, async {}, || stop.set()).await);
    }

    #[tokio::test(start_paused = true)]
    async fn panic_during_drain_and_wedged_collector_fail() {
        let stop = Arc::new(Flag::new());
        let stopped = stop.clone();
        let collector = tokio::spawn(async move {
            stopped.wait().await;
            panic!("secret");
        });
        assert!(supervise(collector, None, None, async {}, || stop.set()).await);
        let start = tokio::time::Instant::now();
        assert!(supervise(tokio::spawn(pending()), None, None, async {}, || {}).await);
        assert_eq!(start.elapsed(), Duration::from_secs(60));
    }
}
