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
