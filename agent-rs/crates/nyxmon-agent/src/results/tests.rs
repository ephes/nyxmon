//! Counterparts of `tests/unit/test_notification_dampening.py`,
//! `test_notification_reminders.py`, `test_notification_delivery_retry.py`
//! and the handler parts of `test_notification_suppression.py`, run against
//! the SQLite store built from the Django schema fixture.

use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};

use nyxmon_core::env::SiteMode;
use nyxmon_core::model::NotificationState;
use nyxmon_core::site::{PathName, PathSnapshot, PathState, SiteSnapshot};
use nyxmon_notify::Delivery;

use super::*;
use crate::logging;
use crate::testutil::{ManualClock, ScriptedNotifier, TestDb, check, dict, handler, result};

const T0: i64 = 1_700_000_000;
const RETRY: i64 = 300;

struct Setup {
    db: TestDb,
    notifier: Arc<ScriptedNotifier>,
    clock: ManualClock,
}

impl Setup {
    fn new() -> Setup {
        let db = TestDb::new();
        let notifier = ScriptedNotifier::new(Some(db.store()));
        Setup {
            db,
            notifier,
            clock: ManualClock::new(T0),
        }
    }

    fn handler(&self, vars: &[(&str, &str)]) -> ResultHandler<Store> {
        handler(self.db.store(), self.notifier.clone(), vars, &self.clock)
    }
}

async fn submit(
    handler: &ResultHandler<impl ResultStore>,
    check: &Check,
    status: ResultStatus,
) -> bool {
    submit_data(handler, check, status, "{}").await
}

async fn submit_data(
    handler: &ResultHandler<impl ResultStore>,
    check: &Check,
    status: ResultStatus,
    data: &str,
) -> bool {
    handler
        .add_check_result(check, result(check.check_id, status, data), false)
        .await
        .unwrap()
}

/// An inserted check, as `uow.store.checks.get(1)` returns it.
fn stored(setup: &Setup, check_id: i64, data: &str) -> Check {
    let mut check = check(check_id);
    check.data = PyJson::Dict(dict(data));
    setup.db.insert(&check);
    check
}

const ERROR: ResultStatus = ResultStatus::Error;
const OK: ResultStatus = ResultStatus::Ok;
const WARNING: ResultStatus = ResultStatus::Warning;

// ----------------------------------------------------------------- dampening

#[tokio::test]
async fn the_first_error_is_stored_but_not_notified() {
    let setup = Setup::new();
    let handler = setup.handler(&[]);
    let check = stored(&setup, 1, "{}");
    assert!(!submit(&handler, &check, ERROR).await);
    assert_eq!(setup.db.results().len(), 1);
    assert_eq!(setup.notifier.count(), 0);
}

#[tokio::test]
async fn the_second_consecutive_error_notifies_once() {
    let setup = Setup::new();
    let handler = setup.handler(&[]);
    let check = stored(&setup, 1, "{}");
    assert!(!submit(&handler, &check, ERROR).await);
    assert!(submit(&handler, &check, ERROR).await);
    assert!(!submit(&handler, &check, ERROR).await);
    assert_eq!(setup.db.results().len(), 3);
    assert_eq!(setup.notifier.count(), 1);
}

#[tokio::test]
async fn an_ok_between_errors_resets_the_sequence() {
    let setup = Setup::new();
    let handler = setup.handler(&[]);
    let check = stored(&setup, 1, "{}");
    for status in [ERROR, OK, ERROR] {
        submit(&handler, &check, status).await;
    }
    assert_eq!(setup.db.results().len(), 3);
    assert_eq!(setup.notifier.count(), 0);
}

#[tokio::test]
async fn the_threshold_is_configurable_and_counts_warnings() {
    let setup = Setup::new();
    let handler = setup.handler(&[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "3")]);
    let check = stored(&setup, 1, "{}");
    submit(&handler, &check, ERROR).await;
    submit(&handler, &check, ERROR).await;
    assert_eq!(setup.notifier.count(), 0);
    submit(&handler, &check, WARNING).await;
    assert_eq!(setup.notifier.count(), 1);

    let setup = Setup::new();
    let handler = setup.handler(&[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")]);
    let check = stored(&setup, 1, "{}");
    submit(&handler, &check, ERROR).await;
    assert_eq!(setup.notifier.count(), 1);
}

#[tokio::test]
async fn a_persistent_failure_sends_periodic_reminders() {
    let setup = Setup::new();
    let handler = setup.handler(&[
        ("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "2"),
        ("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "900"),
    ]);
    let check = stored(&setup, 1, "{}");
    // Initial alert on sample two, one reminder once 900 s have passed.
    for _ in 0..5 {
        submit(&handler, &check, ERROR).await;
        setup.clock.advance(300);
    }
    assert_eq!(setup.notifier.count(), 2);
    assert_eq!(setup.db.results().len(), 5);
}

#[tokio::test]
async fn the_reminder_window_is_measured_from_the_last_notification() {
    let setup = Setup::new();
    let handler = setup.handler(&[
        ("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "5"),
        ("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "600"),
    ]);
    let check = stored(&setup, 1, "{}");
    for _ in 0..4 {
        submit(&handler, &check, ERROR).await;
    }
    assert_eq!(setup.notifier.count(), 0);
    submit(&handler, &check, ERROR).await;
    assert_eq!(setup.notifier.count(), 1);
    setup.clock.advance(599);
    submit(&handler, &check, ERROR).await;
    assert_eq!(setup.notifier.count(), 1);
    setup.clock.advance(1);
    submit(&handler, &check, ERROR).await;
    assert_eq!(setup.notifier.count(), 2);
}

#[tokio::test]
async fn an_immediate_failure_bypasses_the_threshold_but_has_a_cooldown() {
    let setup = Setup::new();
    let handler = setup.handler(&[
        ("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "5"),
        ("NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS", "3600"),
    ]);
    let mut check = check(1);
    check.disabled = true;
    setup.db.insert(&check);
    let immediate = r#"{"notification_immediate": true}"#;
    submit_data(&handler, &check, ERROR, immediate).await;
    assert_eq!(setup.notifier.count(), 1);
    let (_, sent) = &setup.notifier.calls()[0];
    assert_eq!(
        sent.data.keys().collect::<Vec<_>>(),
        ["notification_immediate"]
    );
    submit_data(&handler, &check, ERROR, immediate).await;
    assert_eq!(setup.notifier.count(), 1);
    setup.clock.advance(3600);
    submit_data(&handler, &check, ERROR, immediate).await;
    assert_eq!(setup.notifier.count(), 2);
}

#[tokio::test]
async fn a_forced_notification_bypasses_cooldown_and_suppression() {
    let setup = Setup::new();
    let fetches = Arc::new(AtomicUsize::new(0));
    let counted = fetches.clone();
    let handler = setup
        .handler(&[("NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS", "7200")])
        .with_suppression_fetch(Arc::new(move |_| {
            counted.fetch_add(1, Ordering::SeqCst);
            Box::pin(async { Some(PyJson::Dict(dict(r#"{"last_status": "running"}"#))) })
        }));
    let check = stored(
        &setup,
        1,
        r#"{"notification_suppression": {"url": "https://example.test/m"}}"#,
    );
    for force in [false, true] {
        let attempted = handler
            .add_check_result(
                &check,
                result(1, ERROR, r#"{"notification_immediate": true}"#),
                force,
            )
            .await
            .unwrap();
        assert_eq!(attempted, force);
    }
    assert_eq!(setup.notifier.count(), 1);
    // The forced sample was not fetched for and not annotated.
    assert_eq!(fetches.load(Ordering::SeqCst), 1);
    let (_, sent) = &setup.notifier.calls()[0];
    assert!(!sent.data.contains_key("notification_suppressed"));
    assert_eq!(setup.db.state(1), NotificationState::default());
    assert_eq!(setup.db.results().len(), 2);
}

#[tokio::test]
async fn a_deleted_check_does_not_emit_an_unpersisted_notification() {
    let setup = Setup::new();
    let handler = setup.handler(&[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")]);
    assert!(!submit(&handler, &check(1), ERROR).await);
    assert_eq!(setup.notifier.count(), 0);
    assert!(setup.db.results().is_empty());
}

#[tokio::test]
async fn a_superseded_result_cannot_alert_or_change_state() {
    let setup = Setup::new();
    let handler = setup.handler(&[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")]);
    let mut current = check(1);
    current.status = "processing".to_owned();
    current.processing_started_at = 200;
    setup.db.insert(&current);
    let state = NotificationState {
        failure_count: 4,
        last_attempt_count: 2,
        last_immediate_at: 123,
        ..NotificationState::default()
    };
    setup.db.set_state(1, state);
    let mut snapshot = check(1);
    snapshot.status = "processing".to_owned();
    snapshot.processing_started_at = 100;
    snapshot.claim_started_at = 100;
    snapshot.schedule_next_check(T0);

    assert!(!submit(&handler, &snapshot, ERROR).await);
    assert_eq!(setup.db.state(1), state);
    assert_eq!(setup.db.results().len(), 1);
    assert_eq!(setup.db.schedule(1), ("processing".to_owned(), 0, 200));
    assert_eq!(setup.notifier.count(), 0);
}

#[tokio::test]
async fn notification_state_survives_result_history_pruning() {
    let setup = Setup::new();
    let handler = setup.handler(&[
        ("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "2"),
        ("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "900"),
    ]);
    let check = stored(&setup, 1, "{}");
    submit(&handler, &check, ERROR).await;
    submit(&handler, &check, ERROR).await;
    assert_eq!(setup.notifier.count(), 1);
    setup.db.execute("DELETE FROM check_result");
    for expected in [1, 1, 2] {
        setup.clock.advance(300);
        submit(&handler, &check, ERROR).await;
        assert_eq!(setup.notifier.count(), expected);
    }
}

#[tokio::test]
async fn recovery_resets_the_immediate_alert_cooldown() {
    let setup = Setup::new();
    let handler = setup.handler(&[("NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS", "3600")]);
    let check = stored(&setup, 1, "{}");
    let immediate = r#"{"notification_immediate": true}"#;
    submit_data(&handler, &check, ERROR, immediate).await;
    submit(&handler, &check, OK).await;
    submit_data(&handler, &check, ERROR, immediate).await;
    assert_eq!(setup.notifier.count(), 2);
}

/// `_ConflictingStore`: the first `conflicts` persists with a transition
/// raise `NotificationStateConflict`.
struct ConflictingStore {
    inner: Store,
    conflicts: AtomicUsize,
    attempts: AtomicUsize,
    requests: Mutex<Vec<PersistRequest>>,
}

impl ResultStore for Arc<ConflictingStore> {
    async fn get_notification_state(&self, check_id: i64) -> Result<NotificationState, StoreError> {
        self.inner.get_notification_state(check_id).await
    }

    async fn persist_check_result(&self, request: PersistRequest) -> Result<bool, StoreError> {
        self.attempts.fetch_add(1, Ordering::SeqCst);
        self.requests.lock().unwrap().push(request.clone());
        if request.transition.is_some()
            && self
                .conflicts
                .try_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_sub(1))
                .is_ok()
        {
            return Err(StoreError::NotificationStateConflict {
                check_id: request.check.check_id,
            });
        }
        self.inner.persist_check_result(request).await
    }

    async fn acknowledge_notification_attempt(
        &self,
        check_id: i64,
        attempt_seq: i64,
    ) -> Result<bool, StoreError> {
        self.inner
            .acknowledge_notification_attempt(check_id, attempt_seq)
            .await
    }
}

fn conflicting(setup: &Setup, conflicts: usize) -> Arc<ConflictingStore> {
    Arc::new(ConflictingStore {
        inner: setup.db.store(),
        conflicts: AtomicUsize::new(conflicts),
        attempts: AtomicUsize::new(0),
        requests: Mutex::new(Vec::new()),
    })
}

#[tokio::test]
async fn a_notification_state_conflict_retries_then_persists() {
    let setup = Setup::new();
    let store = conflicting(&setup, 1);
    let handler = handler(store.clone(), setup.notifier.clone(), &[], &setup.clock);
    let check = stored(&setup, 1, "{}");
    let (capture, _guard) = logging::capture();
    submit(&handler, &check, ERROR).await;
    assert_eq!(store.attempts.load(Ordering::SeqCst), 2);
    assert_eq!(setup.db.results().len(), 1);
    assert_eq!(setup.db.state(1).failure_count, 1);
    assert_eq!(
        capture.messages(),
        ["notification state changed concurrently for check_id=1; retrying"]
    );
}

#[tokio::test]
async fn contention_never_discards_the_result() {
    let setup = Setup::new();
    let store = conflicting(&setup, 3);
    let handler = handler(
        store.clone(),
        setup.notifier.clone(),
        &[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")],
        &setup.clock,
    );
    let check = stored(&setup, 1, "{}");
    let (capture, _guard) = logging::capture();
    assert!(!submit(&handler, &check, ERROR).await);
    assert_eq!(store.attempts.load(Ordering::SeqCst), 4);
    assert_eq!(setup.db.results().len(), 1);
    assert_eq!(setup.notifier.count(), 0);
    assert_eq!(setup.db.state(1), NotificationState::default());
    let last = store.requests.lock().unwrap().pop().unwrap();
    assert_eq!((last.transition, last.hold_marker), (None, None));
    assert!(last.complete_check);
    assert_eq!(
        capture
            .lines()
            .last()
            .map(|line| (line.level, line.message.clone())),
        Some((
            "ERROR",
            "notification state remained contended for check_id=1; persisting result without \
             changing alert state"
                .to_owned()
        ))
    );
}

/// A site state whose snapshot blocks the `ipv4` path when told to.
struct StubSite {
    holding: Mutex<bool>,
    clock: ManualClock,
}

impl SiteState for StubSite {
    fn snapshot(&self) -> Option<SiteSnapshot> {
        let state = if *self.holding.lock().unwrap() {
            PathState::Down
        } else {
            PathState::Up
        };
        let mut paths = [None; 3];
        paths[PathName::Ipv4.index()] = Some(PathSnapshot::new(state));
        Some(SiteSnapshot {
            paths,
            observed_at: self.clock.now(),
            mode: SiteMode::Enforce,
            incident_id: 7,
        })
    }

    fn limits(&self) -> SiteLimits {
        SiteLimits {
            stale_after: 180,
            max_hold: 600,
        }
    }
}

const IPV4_DEPENDENCY: &str = r#"{"site_dependency": {"requires": ["ipv4"]}}"#;

#[tokio::test]
async fn the_contention_fallback_keeps_the_hold_marker() {
    let setup = Setup::new();
    let store = conflicting(&setup, 3);
    let site = Arc::new(StubSite {
        holding: Mutex::new(true),
        clock: setup.clock.clone(),
    });
    let handler =
        handler(store.clone(), setup.notifier.clone(), &[], &setup.clock).with_site_state(site);
    let check = stored(&setup, 1, IPV4_DEPENDENCY);
    let (_capture, _guard) = logging::capture();
    submit(&handler, &check, ERROR).await;
    let last = store.requests.lock().unwrap().pop().unwrap();
    assert_eq!((last.transition, last.hold_marker), (None, Some(T0)));
    assert_eq!(setup.db.state(1).held_since, T0);
    // The stored sample carries the hold annotation of every round.
    let (_, _, data) = setup.db.results().pop().unwrap();
    assert_eq!(
        data,
        r#"{"site_connectivity": {"held": true, "reason": "dependency_down", "paths": ["ipv4"], "down_since": 0, "state": "down", "incident_id": 7}}"#
    );
}

#[tokio::test]
async fn a_hold_still_wins_over_a_due_delivery_retry() {
    let setup = Setup::new();
    let site = Arc::new(StubSite {
        holding: Mutex::new(false),
        clock: setup.clock.clone(),
    });
    let handler = setup
        .handler(&[("NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS", "300")])
        .with_site_state(site.clone());
    setup.notifier.always(Delivery::Failed);
    let check = stored(&setup, 1, IPV4_DEPENDENCY);
    let immediate = r#"{"notification_immediate": true}"#;
    submit_data(&handler, &check, ERROR, immediate).await;
    assert_eq!(setup.notifier.count(), 1);
    assert_eq!(setup.db.state(1).attempt_at, T0);

    *site.holding.lock().unwrap() = true;
    setup.clock.advance(RETRY);
    submit_data(&handler, &check, ERROR, immediate).await;
    assert_eq!(setup.notifier.count(), 1, "a held sample was retried");
    let state = setup.db.state(1);
    assert_eq!(state.held_since, T0 + RETRY);
    assert_eq!(state.attempt_at, T0);
}

// ----------------------------------------------------------------- reminders

#[tokio::test]
async fn sample_count_alone_never_triggers_a_reminder() {
    let setup = Setup::new();
    let handler = setup.handler(&[
        ("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1"),
        ("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "3600"),
    ]);
    let check = stored(&setup, 1, "{}");
    for _ in 0..50 {
        submit(&handler, &check, ERROR).await;
    }
    assert_eq!(setup.notifier.count(), 1);
}

#[tokio::test]
async fn reminders_are_interval_independent() {
    let setup = Setup::new();
    let handler = setup.handler(&[
        ("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1"),
        ("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "3600"),
    ]);
    let mut fast = stored(&setup, 1, "{}");
    fast.check_interval = 300;
    let mut slow = check(2);
    slow.check_interval = 3600;
    setup.db.insert(&slow);
    let mut due = [T0, T0];
    let mut sent: [Vec<i64>; 2] = [Vec::new(), Vec::new()];
    while setup.clock.now() <= T0 + 4 * 3600 {
        for (index, check) in [&fast, &slow].into_iter().enumerate() {
            if setup.clock.now() >= due[index] {
                if submit(&handler, check, ERROR).await {
                    sent[index].push(setup.clock.now() - T0);
                }
                due[index] += check.check_interval;
            }
        }
        setup.clock.advance(60);
    }
    assert_eq!(sent[0].len(), 5);
    assert_eq!(sent[1].len(), 5);
    for (fast_at, slow_at) in sent[0].iter().zip(&sent[1]) {
        assert!((fast_at - slow_at).abs() <= 3600);
    }
    assert!(sent[1][4] >= 3 * 3600);
}

#[tokio::test]
async fn the_deprecated_repeat_failures_variable_is_ignored_with_a_warning() {
    let setup = Setup::new();
    let (env, sink) = crate::testutil::env(&[
        ("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1"),
        ("NYXMON_NOTIFY_REPEAT_FAILURES", "3"),
    ]);
    let handler = ResultHandler::new(
        setup.db.store(),
        setup.notifier.clone(),
        env,
        Arc::new(WarnOnce::new(sink.clone())),
    )
    .with_clock(setup.clock.clock());
    let check = stored(&setup, 1, "{}");
    for _ in 0..10 {
        submit(&handler, &check, ERROR).await;
        setup.clock.advance(300);
    }
    assert_eq!(setup.notifier.count(), 1);
    let warnings = sink.take();
    assert_eq!(warnings.len(), 1);
    assert!(warnings[0].1.contains("NYXMON_NOTIFY_REPEAT_FAILURES"));
    assert!(
        warnings[0]
            .1
            .contains("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS")
    );
}

#[tokio::test]
async fn a_per_check_policy_overrides_threshold_and_reminder() {
    let setup = Setup::new();
    let handler = setup.handler(&[("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "21600")]);
    let check = stored(
        &setup,
        1,
        r#"{"notification_policy": {"consecutive_failures": 1, "reminder_seconds": 600}}"#,
    );
    submit(&handler, &check, ERROR).await;
    assert_eq!(setup.notifier.count(), 1);
    setup.clock.advance(599);
    submit(&handler, &check, ERROR).await;
    assert_eq!(setup.notifier.count(), 1);
    setup.clock.advance(1);
    submit(&handler, &check, ERROR).await;
    assert_eq!(setup.notifier.count(), 2);
}

#[tokio::test]
async fn a_malformed_per_check_policy_falls_back_to_the_global_default() {
    for policy in [
        r#""not-a-dict""#,
        r#"{"consecutive_failures": 0}"#,
        r#"{"consecutive_failures": true}"#,
        r#"{"consecutive_failures": "many"}"#,
        r#"{"reminder_seconds": -1}"#,
        r#"{"consecutive_failures": 1000000000}"#,
    ] {
        let setup = Setup::new();
        let (env, sink) = crate::testutil::env(&[]);
        let handler = ResultHandler::new(
            setup.db.store(),
            setup.notifier.clone(),
            env,
            Arc::new(WarnOnce::new(sink.clone())),
        )
        .with_clock(setup.clock.clock());
        let check = stored(
            &setup,
            1,
            &format!(r#"{{"notification_policy": {policy}}}"#),
        );
        submit(&handler, &check, ERROR).await;
        assert_eq!(setup.notifier.count(), 0, "{policy}");
        submit(&handler, &check, ERROR).await;
        assert_eq!(setup.notifier.count(), 1, "{policy}");
        let warnings = sink.take();
        assert_eq!(warnings.len(), 1, "{policy}: {warnings:?}");
        assert!(warnings[0].1.contains("notification_policy"));
        assert!(warnings[0].1.contains("using the global default"));
    }
}

#[tokio::test]
async fn warnings_use_the_warning_reminder_cadence() {
    let setup = Setup::new();
    let handler = setup.handler(&[
        ("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1"),
        ("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "3600"),
        ("NYXMON_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS", "86400"),
    ]);
    let check = stored(&setup, 1, "{}");
    submit(&handler, &check, WARNING).await;
    assert_eq!(setup.notifier.count(), 1);
    setup.clock.advance(3600 * 5);
    submit(&handler, &check, WARNING).await;
    assert_eq!(setup.notifier.count(), 1);
    setup.clock.advance(86400);
    submit(&handler, &check, WARNING).await;
    assert_eq!(setup.notifier.count(), 2);
}

#[tokio::test]
async fn a_recovered_check_starts_a_new_incident() {
    let setup = Setup::new();
    let handler = setup.handler(&[
        ("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1"),
        ("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "86400"),
    ]);
    let check = stored(&setup, 1, "{}");
    submit(&handler, &check, ERROR).await;
    setup.clock.advance(300);
    submit(&handler, &check, OK).await;
    let recovered = setup.db.state(1);
    assert_eq!(
        recovered,
        NotificationState {
            attempt_seq: recovered.attempt_seq,
            ..NotificationState::default()
        }
    );
    assert_eq!(recovered.attempt_seq, 1);
    setup.clock.advance(300);
    submit(&handler, &check, ERROR).await;
    assert_eq!(setup.notifier.count(), 2);
}

#[tokio::test]
async fn notification_state_survives_a_restart_and_retention_cleanup() {
    let setup = Setup::new();
    let vars = [
        ("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1"),
        ("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "3600"),
    ];
    let check = stored(&setup, 1, "{}");
    submit(&setup.handler(&vars), &check, ERROR).await;
    assert_eq!(setup.notifier.count(), 1);
    setup.db.execute("DELETE FROM check_result");

    // A new process: a fresh store and handler over the same file.
    let restarted = setup.handler(&vars);
    setup.clock.advance(600);
    submit(&restarted, &check, ERROR).await;
    assert_eq!(setup.notifier.count(), 1);
    setup.clock.advance(3000);
    submit(&restarted, &check, ERROR).await;
    assert_eq!(setup.notifier.count(), 2);
}

// ------------------------------------------------------------ delivery retry

fn paging(setup: &Setup, retry: Option<&str>) -> ResultHandler<Store> {
    let mut vars = vec![
        ("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1"),
        ("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "86400"),
    ];
    if let Some(retry) = retry {
        vars.push(("NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS", retry));
    }
    setup.handler(&vars)
}

#[tokio::test]
async fn the_intent_is_persisted_before_the_notifier_is_called() {
    let setup = Setup::new();
    let handler = paging(&setup, None);
    let check = stored(&setup, 1, "{}");
    submit(&handler, &check, ERROR).await;
    let at_send = setup.notifier.states_at_send()[0];
    assert_eq!((at_send.attempt_seq, at_send.attempt_at), (1, T0));
    let after = setup.db.state(1);
    assert_eq!((after.attempt_seq, after.attempt_at), (1, 0));
}

#[tokio::test]
async fn a_failed_send_leaves_the_intent_pending_and_unknown_counts_as_delivered() {
    let setup = Setup::new();
    let handler = paging(&setup, None);
    let check = stored(&setup, 1, "{}");
    setup.notifier.always(Delivery::Failed);
    submit(&handler, &check, ERROR).await;
    assert_eq!(setup.db.state(1).attempt_at, T0);

    let setup = Setup::new();
    let handler = paging(&setup, None);
    let check = stored(&setup, 1, "{}");
    setup.notifier.always(Delivery::Unknown);
    submit(&handler, &check, ERROR).await;
    assert_eq!(setup.db.state(1).attempt_at, 0);
}

#[tokio::test]
async fn the_acknowledgement_is_fenced_by_the_attempt_sequence() {
    let setup = Setup::new();
    let handler = paging(&setup, None);
    let check = stored(&setup, 1, "{}");
    setup.notifier.always(Delivery::Failed);
    submit(&handler, &check, ERROR).await;
    submit(&handler, &check, OK).await;
    let recovered = setup.db.state(1);
    assert_eq!((recovered.attempt_seq, recovered.attempt_at), (1, 0));
    submit(&handler, &check, ERROR).await;
    let pending = setup.db.state(1);
    assert_eq!((pending.attempt_seq, pending.attempt_at), (2, T0));
    let store = setup.db.store();
    assert!(!store.acknowledge_notification_attempt(1, 1).await.unwrap());
    assert_eq!(setup.db.state(1).attempt_at, T0);
}

#[tokio::test]
async fn an_ok_sample_clears_a_pending_intent() {
    let setup = Setup::new();
    let handler = paging(&setup, Some("300"));
    let check = stored(&setup, 1, "{}");
    setup.notifier.always(Delivery::Failed);
    submit(&handler, &check, ERROR).await;
    setup.clock.advance(RETRY + 1);
    setup.notifier.always(Delivery::Delivered);
    submit(&handler, &check, OK).await;
    setup.clock.advance(RETRY + 1);
    submit(&handler, &check, OK).await;
    assert_eq!(setup.notifier.count(), 1);
    assert_eq!(setup.db.state(1).attempt_at, 0);
}

#[tokio::test]
async fn an_ambiguous_outcome_repeats_once_then_stops() {
    let setup = Setup::new();
    let handler = paging(&setup, Some("300"));
    let check = stored(&setup, 1, "{}");
    setup.notifier.set_outcome(|call| {
        Ok(if call == 1 {
            Delivery::Failed
        } else {
            Delivery::Delivered
        })
    });
    submit(&handler, &check, ERROR).await;
    setup.clock.advance(RETRY - 1);
    submit(&handler, &check, ERROR).await;
    assert_eq!(setup.notifier.count(), 1);
    setup.clock.advance(1);
    submit(&handler, &check, ERROR).await;
    assert_eq!(setup.notifier.count(), 2);
    assert_eq!(setup.db.state(1).attempt_at, 0);
    for _ in 0..5 {
        setup.clock.advance(RETRY + 1);
        submit(&handler, &check, ERROR).await;
    }
    assert_eq!(setup.notifier.count(), 2);
}

#[tokio::test]
async fn the_knob_at_zero_keeps_a_failed_alert_lost_until_the_reminder() {
    let setup = Setup::new();
    let handler = paging(&setup, None);
    let check = stored(&setup, 1, "{}");
    setup.notifier.always(Delivery::Failed);
    submit(&handler, &check, ERROR).await;
    for _ in 0..10 {
        setup.clock.advance(RETRY * 2);
        submit(&handler, &check, ERROR).await;
    }
    assert_eq!(setup.notifier.count(), 1);
    assert_eq!(setup.db.state(1).attempt_at, T0);
}

#[tokio::test]
async fn acks_written_with_the_knob_off_prevent_a_later_resend() {
    let setup = Setup::new();
    let check = stored(&setup, 1, "{}");
    submit(&paging(&setup, None), &check, ERROR).await;
    assert_eq!(setup.db.state(1).attempt_at, 0);
    let enabled = paging(&setup, Some("300"));
    for _ in 0..5 {
        setup.clock.advance(RETRY + 1);
        submit(&enabled, &check, ERROR).await;
    }
    assert_eq!(setup.notifier.count(), 1);
}

#[tokio::test]
async fn the_retry_ignores_the_reminder_window() {
    let setup = Setup::new();
    let handler = paging(&setup, Some("300"));
    let check = stored(&setup, 1, "{}");
    setup.notifier.always(Delivery::Failed);
    submit(&handler, &check, ERROR).await;
    for _ in 0..3 {
        setup.clock.advance(RETRY);
        submit(&handler, &check, ERROR).await;
    }
    assert_eq!(setup.notifier.count(), 4);
    let state = setup.db.state(1);
    assert_eq!(
        (state.attempt_seq, state.attempt_at),
        (4, setup.clock.now())
    );
}

#[tokio::test]
async fn an_unacknowledged_immediate_alert_is_repeated_inside_the_cooldown() {
    let setup = Setup::new();
    let handler = paging(&setup, Some("300"));
    let check = stored(&setup, 1, "{}");
    let immediate = r#"{"notification_immediate": true}"#;
    setup.notifier.always(Delivery::Failed);
    submit_data(&handler, &check, ERROR, immediate).await;
    setup.clock.advance(RETRY);
    submit_data(&handler, &check, ERROR, immediate).await;
    assert_eq!(setup.notifier.count(), 2);
    assert_eq!(setup.db.state(1).attempt_seq, 2);
    setup.notifier.always(Delivery::Delivered);
    setup.clock.advance(RETRY);
    submit_data(&handler, &check, ERROR, immediate).await;
    assert_eq!(setup.notifier.count(), 3);
    assert_eq!(setup.db.state(1).attempt_at, 0);
    for _ in 0..3 {
        setup.clock.advance(RETRY);
        submit_data(&handler, &check, ERROR, immediate).await;
    }
    assert_eq!(setup.notifier.count(), 3);

    // With the knob off a failed immediate send waits out the cooldown.
    let setup = Setup::new();
    let handler = paging(&setup, None);
    let check = stored(&setup, 1, "{}");
    setup.notifier.always(Delivery::Failed);
    for _ in 0..6 {
        submit_data(&handler, &check, ERROR, immediate).await;
        setup.clock.advance(RETRY);
    }
    assert_eq!(setup.notifier.count(), 1);
    assert_eq!(setup.db.state(1).attempt_at, T0);
}

/// Acknowledgements fail like a process dying before the ack.
struct FailingAck(Store);

impl ResultStore for FailingAck {
    async fn get_notification_state(&self, check_id: i64) -> Result<NotificationState, StoreError> {
        self.0.get_notification_state(check_id).await
    }

    async fn persist_check_result(&self, request: PersistRequest) -> Result<bool, StoreError> {
        self.0.persist_check_result(request).await
    }

    async fn acknowledge_notification_attempt(&self, _: i64, _: i64) -> Result<bool, StoreError> {
        Err(StoreError::ActorDied)
    }
}

#[tokio::test]
async fn a_lost_acknowledgement_retries_after_a_restart() {
    let setup = Setup::new();
    let vars = [
        ("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1"),
        ("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "86400"),
        ("NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS", "300"),
    ];
    let check = stored(&setup, 1, "{}");
    let crashing = handler(
        FailingAck(setup.db.store()),
        setup.notifier.clone(),
        &vars,
        &setup.clock,
    );
    let (capture, _guard) = logging::capture();
    assert!(submit(&crashing, &check, ERROR).await);
    assert!(capture.contains("failed to acknowledge notification attempt 1 for check_id=1"));
    assert_eq!(setup.db.state(1).attempt_at, T0);

    setup.clock.advance(RETRY + 1);
    submit(&setup.handler(&vars), &check, ERROR).await;
    assert_eq!(setup.notifier.count(), 2);
    let state = setup.db.state(1);
    assert_eq!((state.attempt_seq, state.attempt_at), (2, 0));
}

// --------------------------------------------------------------- suppression

const SUPPRESSED: &str =
    r#"{"notification_suppression": {"url": "https://example.test/m", "reason": "backup"}}"#;

fn serving(payload: Option<&'static str>) -> SuppressionFetch {
    Arc::new(move |request| {
        assert_eq!(request.url, "https://example.test/m");
        Box::pin(async move { payload.map(|text| PyJson::Dict(dict(text))) })
    })
}

#[tokio::test]
async fn a_suppressed_result_is_persisted_without_notification() {
    let setup = Setup::new();
    let handler = setup
        .handler(&[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")])
        .with_suppression_fetch(serving(Some(r#"{"last_status": "running"}"#)));
    let check = stored(&setup, 1, SUPPRESSED);
    assert!(!submit_data(&handler, &check, ERROR, r#"{"error_type": "timeout"}"#).await);
    assert_eq!(setup.notifier.count(), 0);
    assert_eq!(
        setup.db.results(),
        [(
            1,
            "error".to_owned(),
            r#"{"error_type": "timeout", "notification_suppressed": {"reason": "backup", "source_url": "https://example.test/m", "source_status": "running"}}"#
                .to_owned()
        )]
    );
    // An OK sample is never fetched for.
    let handler = handler.with_suppression_fetch(Arc::new(|_| panic!("fetched for OK")));
    submit(&handler, &check, OK).await;
}

#[tokio::test]
async fn a_suppressed_result_breaks_the_failure_streak() {
    let setup = Setup::new();
    let check = stored(&setup, 1, SUPPRESSED);
    let plain = setup.handler(&[]);
    let suppressed = setup
        .handler(&[])
        .with_suppression_fetch(serving(Some(r#"{"last_status": "running"}"#)));
    submit(&plain, &check, ERROR).await;
    assert_eq!(setup.db.state(1).failure_count, 1);
    submit(&suppressed, &check, ERROR).await;
    assert_eq!(setup.db.state(1).failure_count, 0);
    submit(&plain, &check, ERROR).await;
    assert_eq!(setup.notifier.count(), 0);
    submit(&plain, &check, ERROR).await;
    assert_eq!(setup.notifier.count(), 1);
}

#[tokio::test]
async fn suppression_fails_open_without_a_payload_or_a_match() {
    for payload in [None, Some(r#"{"last_status": "idle"}"#)] {
        let setup = Setup::new();
        let handler = setup
            .handler(&[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")])
            .with_suppression_fetch(serving(payload));
        let check = stored(&setup, 1, SUPPRESSED);
        assert!(submit(&handler, &check, ERROR).await);
        assert_eq!(setup.db.results()[0].2, "{}");
    }
}

#[tokio::test]
async fn collector_internal_samples_are_not_fetched_for() {
    let setup = Setup::new();
    let handler = setup
        .handler(&[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")])
        .with_suppression_fetch(Arc::new(|_| panic!("fetched for an internal sample")));
    let check = stored(&setup, 1, SUPPRESSED);
    assert!(!submit_data(&handler, &check, ERROR, r#"{"collector_internal": true}"#).await);
    assert_eq!(setup.db.state(1), NotificationState::default());
}

#[tokio::test]
async fn a_raising_suppression_config_fails_the_result_handling() {
    let setup = Setup::new();
    let handler = setup.handler(&[]);
    // `float(10**400)` raises OverflowError in `_timeout_seconds`.
    let check = stored(
        &setup,
        1,
        &format!(
            r#"{{"notification_suppression": {{"url": "https://x.test", "timeout": 1{}}}}}"#,
            "0".repeat(400)
        ),
    );
    let (capture, _guard) = logging::capture();
    handler.receive(check, result(1, ERROR, "{}")).await;
    assert!(setup.db.results().is_empty());
    let lines = capture.lines();
    assert_eq!(lines.len(), 1);
    assert_eq!(lines[0].logger, "nyxmon.service_layer.handlers");
    assert!(
        lines[0]
            .message
            .starts_with("result handling failed for check_id=1\nOverflowError: ")
    );
}

// ------------------------------------------------------------ result_received

#[tokio::test]
async fn receive_schedules_the_next_run_and_completes_the_claim() {
    let setup = Setup::new();
    let handler = setup.handler(&[]);
    let mut check = check(1);
    check.status = "processing".to_owned();
    check.processing_started_at = T0 - 5;
    check.claim_started_at = T0 - 5;
    setup.db.insert(&check);
    handler.receive(check, result(1, OK, "{}")).await;
    assert_eq!(setup.db.schedule(1), ("idle".to_owned(), T0 + 300, 0));
    assert_eq!(setup.db.results().len(), 1);
}

#[tokio::test]
async fn a_raising_notifier_fails_the_result_handling_without_ack() {
    let setup = Setup::new();
    let handler = paging(&setup, None);
    let check = stored(&setup, 1, "{}");
    setup.notifier.set_outcome(|_| {
        Err(NotifyRaised {
            class: "OverflowError",
            message: "date value out of range".to_owned(),
        })
    });
    let (capture, _guard) = logging::capture();
    handler.receive(check, result(1, ERROR, "{}")).await;
    assert_eq!(
        capture.messages(),
        ["result handling failed for check_id=1\nOverflowError: date value out of range"]
    );
    // Persisted with the intent, which stays pending.
    assert_eq!(setup.db.results().len(), 1);
    assert_eq!(setup.db.state(1).attempt_at, T0);
}
