//! Counterparts of `tests/unit/test_shutdown_claims.py` (collector parts)
//! and `test_shutdown_flush.py::test_result_queued_behind_slow_alert_is_recorded_at_shutdown`,
//! plus the basic claim/execute/sleep loop, against the SQLite store.

use std::time::Instant;

use nyxmon_checks::ctx::BatchCtx;
use nyxmon_checks::registry::ExecFuture;
use nyxmon_core::model::{CheckResult, ResultStatus};
use nyxmon_core::pyjson::PyDict;
use nyxmon_notify::{Delivery, Notifier, NotifyFuture};
use nyxmon_store::StoreOptions;

use super::*;
use crate::logging;
use crate::testutil::{ManualClock, ScriptedNotifier, TestDb, check, env, handler};

pub(super) const T0: i64 = 1_700_000_000;

pub(super) type Behaviour = dyn Fn(Check) -> ExecFuture + Send + Sync;

pub(super) struct FakeExecutors(pub(super) Box<Behaviour>);

impl Executors for FakeExecutors {
    fn registered_types(&self) -> &[&'static str] {
        &["http"]
    }

    fn execute(&self, check: Check, _batch: Arc<BatchCtx>) -> Option<ExecFuture> {
        Some((self.0)(check))
    }
}

pub(super) fn result(check: &Check, status: ResultStatus) -> CheckResult {
    CheckResult::new(check.check_id, status, PyDict::new())
}

pub(super) struct Setup {
    pub(super) db: TestDb,
    pub(super) clock: ManualClock,
}

impl Setup {
    pub(super) fn new(checks: &[i64]) -> Setup {
        let db = TestDb::new();
        for check_id in checks {
            db.insert(&check(*check_id));
        }
        Setup {
            db,
            clock: ManualClock::new(T0),
        }
    }

    pub(super) fn collector(
        &self,
        vars: &[(&str, &str)],
        notifier: Arc<dyn Notifier>,
        behaviour: impl Fn(Check) -> ExecFuture + Send + Sync + 'static,
    ) -> Arc<Collector> {
        self.collector_on(self.db.store(), vars, notifier, behaviour)
    }

    pub(super) fn collector_on(
        &self,
        store: Store,
        vars: &[(&str, &str)],
        notifier: Arc<dyn Notifier>,
        behaviour: impl Fn(Check) -> ExecFuture + Send + Sync + 'static,
    ) -> Arc<Collector> {
        Arc::new(self.build(store, vars, notifier, behaviour))
    }

    /// A collector on `store` (a fresh process when it is a new store).
    pub(super) fn build(
        &self,
        store: Store,
        vars: &[(&str, &str)],
        notifier: Arc<dyn Notifier>,
        behaviour: impl Fn(Check) -> ExecFuture + Send + Sync + 'static,
    ) -> Collector {
        let handler = Arc::new(handler(store.clone(), notifier, vars, &self.clock));
        let (env, _) = env(vars);
        Collector::new(
            store,
            handler,
            Arc::new(FakeExecutors(Box::new(behaviour))),
            env,
            3600,
        )
    }
}

pub(super) async fn until(condition: impl Fn() -> bool) {
    tokio::time::timeout(Duration::from_secs(5), async {
        while !condition() {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("condition not reached");
}

pub(super) async fn stop_and_join(
    collector: &Arc<Collector>,
    run: tokio::task::JoinHandle<()>,
) -> Duration {
    let started = Instant::now();
    collector.request_stop();
    tokio::time::timeout(Duration::from_secs(10), run)
        .await
        .expect("shutdown hung")
        .unwrap();
    started.elapsed()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn due_checks_are_claimed_executed_and_recorded_and_idle_shutdown_is_prompt() {
    let setup = Setup::new(&[1, 2]);
    let collector = setup.collector(
        &[("NYXMON_SHUTDOWN_GRACE_SECONDS", "5")],
        ScriptedNotifier::new(None),
        |check| Box::pin(async move { Ok(result(&check, ResultStatus::Ok)) }),
    );
    let run = tokio::spawn(collector.clone().run());
    until(|| setup.db.results().len() == 2).await;
    // Nothing is in flight once both results are recorded.
    until(|| collector.active().is_empty()).await;
    assert!(stop_and_join(&collector, run).await < Duration::from_secs(1));
    for check_id in [1, 2] {
        assert_eq!(
            setup.db.schedule(check_id),
            ("idle".to_owned(), T0 + 300, 0)
        );
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn shutdown_releases_an_in_flight_claim_without_a_result() {
    let setup = Setup::new(&[1]);
    let started = Arc::new(Flag::new());
    let collector = setup.collector(
        &[("NYXMON_SHUTDOWN_GRACE_SECONDS", "0")],
        ScriptedNotifier::new(None),
        {
            let started = started.clone();
            move |_| {
                let started = started.clone();
                Box::pin(async move {
                    started.set();
                    std::future::pending().await
                })
            }
        },
    );
    let run = tokio::spawn(collector.clone().run());
    started.wait().await;
    assert_eq!(setup.db.schedule(1), ("processing".to_owned(), 0, T0));
    stop_and_join(&collector, run).await;
    assert_eq!(setup.db.schedule(1), ("idle".to_owned(), T0 + 300, 0));
    assert!(setup.db.results().is_empty());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_result_finishing_within_the_grace_is_recorded_promptly() {
    let setup = Setup::new(&[1]);
    let started = Arc::new(Flag::new());
    let collector = setup.collector(
        &[("NYXMON_SHUTDOWN_GRACE_SECONDS", "5")],
        ScriptedNotifier::new(None),
        {
            let started = started.clone();
            move |check| {
                let started = started.clone();
                Box::pin(async move {
                    started.set();
                    tokio::time::sleep(Duration::from_millis(200)).await;
                    Ok(result(&check, ResultStatus::Ok))
                })
            }
        },
    );
    let run = tokio::spawn(collector.clone().run());
    started.wait().await;
    let elapsed = stop_and_join(&collector, run).await;
    assert!(elapsed < Duration::from_secs(2), "{elapsed:?}");
    assert_eq!(setup.db.results(), [(1, "ok".to_owned(), "{}".to_owned())]);
    assert_eq!(setup.db.schedule(1), ("idle".to_owned(), T0 + 300, 0));
}

/// Takes `delay` to deliver.
struct SlowNotifier {
    delay: Duration,
    started: Arc<Flag>,
}

impl Notifier for SlowNotifier {
    fn notify_check_failed<'a>(&'a self, _: &'a Check, _: &'a CheckResult) -> NotifyFuture<'a> {
        Box::pin(async move {
            self.started.set();
            tokio::time::sleep(self.delay).await;
            Ok(Delivery::Delivered)
        })
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_result_queued_behind_a_slow_alert_is_recorded_at_shutdown() {
    let setup = Setup::new(&[1, 2, 3]);
    let alert_started = Arc::new(Flag::new());
    let stopping = Arc::new(Flag::new());
    let notifier = Arc::new(SlowNotifier {
        delay: Duration::from_millis(600),
        started: alert_started.clone(),
    });
    let collector = setup.collector(
        &[
            ("NYXMON_SHUTDOWN_GRACE_SECONDS", "0.2"),
            ("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1"),
        ],
        notifier,
        {
            let stopping = stopping.clone();
            move |check| {
                let stopping = stopping.clone();
                Box::pin(async move {
                    match check.check_id {
                        1 => Ok(result(&check, ResultStatus::Error)),
                        2 => {
                            // Finishes within the grace, while check 1 alerts.
                            stopping.wait().await;
                            tokio::time::sleep(Duration::from_millis(50)).await;
                            Ok(result(&check, ResultStatus::Ok))
                        }
                        _ => std::future::pending().await,
                    }
                })
            }
        },
    );
    let run = tokio::spawn(collector.clone().run());
    alert_started.wait().await;
    stopping.set();
    stop_and_join(&collector, run).await;
    let mut recorded: Vec<i64> = setup.db.results().iter().map(|row| row.0).collect();
    recorded.sort_unstable();
    assert_eq!(recorded, [1, 2]);
    assert_eq!(setup.db.schedule(3), ("idle".to_owned(), T0 + 300, 0));
}

#[tokio::test]
async fn results_not_recorded_within_the_flush_window_are_given_up() {
    let setup = Setup::new(&[1, 2]);
    let alert_started = Arc::new(Flag::new());
    let notifier = Arc::new(SlowNotifier {
        delay: Duration::from_secs(30),
        started: alert_started.clone(),
    });
    let collector = Arc::new(
        Arc::into_inner(setup.collector(
            &[
                ("NYXMON_SHUTDOWN_GRACE_SECONDS", "0"),
                ("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1"),
            ],
            notifier,
            |check| {
                Box::pin(async move {
                    if check.check_id == 1 {
                        Ok(result(&check, ResultStatus::Error))
                    } else {
                        std::future::pending().await
                    }
                })
            },
        ))
        .unwrap()
        .with_flush_timeout(Duration::from_millis(100)),
    );
    let (capture, _guard) = logging::capture();
    let run = tokio::spawn(collector.clone().run());
    alert_started.wait().await;
    stop_and_join(&collector, run).await;
    assert!(capture.contains(
        "results that finished during the shutdown grace period were not all recorded within \
         15s; releasing the remaining leases"
    ));
    // Check 1 was recorded before its alert; check 2 is released.
    assert_eq!(setup.db.results().len(), 1);
    assert_eq!(setup.db.schedule(1), ("idle".to_owned(), T0 + 300, 0));
    assert_eq!(setup.db.schedule(2), ("idle".to_owned(), T0 + 300, 0));
}

#[tokio::test]
async fn a_failed_lease_release_is_logged_and_does_not_hang_shutdown() {
    let setup = Setup::new(&[1]);
    let started = Arc::new(Flag::new());
    let store = Store::open_with(
        &setup.db.path,
        StoreOptions {
            busy_timeout: Duration::from_millis(50),
        },
    )
    .unwrap();
    let collector = setup.collector_on(
        store,
        &[("NYXMON_SHUTDOWN_GRACE_SECONDS", "0")],
        ScriptedNotifier::new(None),
        {
            let started = started.clone();
            move |_| {
                let started = started.clone();
                Box::pin(async move {
                    started.set();
                    std::future::pending().await
                })
            }
        },
    );
    let (capture, _guard) = logging::capture();
    let run = tokio::spawn(collector.clone().run());
    started.wait().await;
    let locker = setup.db.raw();
    locker.execute_batch("BEGIN EXCLUSIVE").unwrap();
    stop_and_join(&collector, run).await;
    locker.execute_batch("ROLLBACK").unwrap();
    assert!(
        capture
            .messages()
            .iter()
            .any(|line| line.starts_with("failed to release check leases on shutdown\n"))
    );
    assert_eq!(setup.db.schedule(1), ("processing".to_owned(), 0, T0));
}

#[tokio::test]
async fn a_claim_failure_is_logged_and_the_loop_continues() {
    let setup = Setup::new(&[1]);
    let store = Store::open_with(
        &setup.db.path,
        StoreOptions {
            busy_timeout: Duration::from_millis(50),
        },
    )
    .unwrap();
    let collector = setup.collector_on(store, &[], ScriptedNotifier::new(None), |check| {
        Box::pin(async move { Ok(result(&check, ResultStatus::Ok)) })
    });
    let locker = setup.db.raw();
    locker.execute_batch("BEGIN EXCLUSIVE").unwrap();
    let (capture, _guard) = logging::capture();
    collector.collect_once(&Flag::new()).await;
    locker.execute_batch("ROLLBACK").unwrap();
    assert!(
        capture
            .messages()
            .iter()
            .any(|line| line.starts_with("check collector iteration failed\n"))
    );
    assert!(!collector.failed());
    collector.collect_once(&Flag::new()).await;
    assert_eq!(setup.db.results().len(), 1);
}
