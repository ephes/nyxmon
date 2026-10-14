//! Counterparts of `tests/unit/test_collector_recovery.py`,
//! `tests/unit/test_collector_incidents.py` and
//! `test_shutdown_claims.py::test_shutdown_during_stale_recovery_keeps_results_and_alert`
//! (PLAN.md 5.8 steps 1-5 and 7), against the SQLite store with a manual
//! clock. A wedged batch is a fake executor that waits on a gate; its
//! deadline is shortened with `with_batch_deadline`.

use std::sync::atomic::{AtomicUsize, Ordering};

use nyxmon_checks::registry::ExecFuture;
use nyxmon_core::collector_incident::{PAUSED_INCIDENT_KEY, STALE_BATCH_INCIDENT_KEY};
use nyxmon_core::model::{CollectorIncident, NotificationState, ResultStatus};
use nyxmon_core::pyjson::{PyDict, PyJson};
use nyxmon_notify::{Delivery, NotifyRaised};
use nyxmon_store::StoreOptions;

use super::incidents::{
    ABANDONED_BATCH_REMINDER_SECONDS, ABANDONED_BATCH_RETRY_SECONDS, STALE_BATCH_REMINDER_SECONDS,
    STALE_BATCH_RESOLVE_AFTER_SECONDS,
};
use super::tests::{Setup, T0, result, stop_and_join, until};
use super::*;
use crate::logging;
use crate::testutil::{ScriptedNotifier, check, dict};

const LEASE_SECONDS: i64 = 900;
/// Keeps a wedged check's own lease from expiring during a test.
const LONG_LEASE: (&str, &str) = ("NYXMON_PROCESSING_LEASE_SECONDS", "1000000");

/// A check whose processing lease expired long ago (`_wedged_check`).
fn wedged(check_id: i64) -> Check {
    Check {
        status: "processing".to_owned(),
        processing_started_at: T0 - 10 * LEASE_SECONDS,
        next_check_time: T0 + 300,
        ..check(check_id)
    }
}

fn ok_executor(check: Check) -> ExecFuture {
    Box::pin(async move { Ok(result(&check, ResultStatus::Ok)) })
}

async fn incident(setup: &Setup, key: &str) -> Option<CollectorIncident> {
    setup.db.store().get_collector_incident(key).await.unwrap()
}

fn retry_pending(collector: &Collector, key: &str) -> bool {
    locked(&collector.retry_keys).contains(key)
}

/// Mark a check as claimed long ago again.
fn restale(setup: &Setup, check_id: i64) {
    setup.db.execute(&format!(
        "UPDATE health_check SET status = 'processing', processing_started_at = {} WHERE id = \
         {check_id}",
        T0 - 10 * LEASE_SECONDS
    ));
}

fn error_type(result: &CheckResult) -> Option<&str> {
    result.data.get("error_type").and_then(PyJson::as_str)
}

/// A batch that waits until its gate opens; `close` arms a new gate.
#[derive(Clone, Default)]
struct Wedge {
    gate: Arc<Mutex<Arc<Flag>>>,
    calls: Arc<AtomicUsize>,
}

impl Wedge {
    fn open(&self) {
        locked(&self.gate).set();
    }

    fn close(&self) {
        *locked(&self.gate) = Arc::new(Flag::new());
    }

    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }

    fn executor(&self) -> impl Fn(Check) -> ExecFuture + Send + Sync + 'static {
        let wedge = self.clone();
        move |check| {
            wedge.calls.fetch_add(1, Ordering::SeqCst);
            let gate = locked(&wedge.gate).clone();
            Box::pin(async move {
                gate.wait().await;
                Ok(result(&check, ResultStatus::Ok))
            })
        }
    }
}

fn wedged_collector(setup: &Setup, notifier: Arc<ScriptedNotifier>, wedge: &Wedge) -> Collector {
    setup
        .build(setup.db.store(), &[LONG_LEASE], notifier, wedge.executor())
        .with_batch_deadline(Duration::from_millis(50))
}

async fn wait_abandoned_done(collector: &Collector) {
    until(|| !collector.abandoned_running()).await;
}

// ------------------------------------------------- test_collector_recovery.py

#[tokio::test]
async fn one_stale_result_failure_does_not_block_other_recoveries() {
    let setup = Setup::new(&[]);
    setup.db.insert(&wedged(1));
    setup.db.insert(&wedged(2));
    setup.db.execute(
        "CREATE TRIGGER fail_one BEFORE INSERT ON check_result WHEN NEW.health_check_id = 1
         BEGIN SELECT RAISE(ABORT, 'database unavailable'); END;",
    );
    let notifier = ScriptedNotifier::new(None);
    let collector = setup.build(setup.db.store(), &[], notifier.clone(), ok_executor);
    let (capture, _guard) = logging::capture();
    collector.collect_once(&Flag::new()).await;
    assert!(capture.messages().iter().any(|line| {
        line.starts_with("failed to record expired processing lease for check_id=1\n")
    }));
    let results = setup.db.results();
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].0, 2);
    assert!(
        results[0]
            .2
            .contains("\"error_type\": \"stale_processing_lease\"")
    );
    // Only the recorded check counts as recovered.
    let incident = incident(&setup, STALE_BATCH_INCIDENT_KEY).await.unwrap();
    assert_eq!(
        incident.payload.get("reclaimed_count"),
        Some(&PyJson::Int(1))
    );
}

#[tokio::test]
async fn a_reclaim_failure_does_not_block_due_checks() {
    let setup = Setup::new(&[7]);
    setup.db.insert(&wedged(1));
    setup.db.execute(
        "CREATE TRIGGER no_reclaim BEFORE UPDATE OF status ON health_check
         WHEN OLD.status = 'processing' AND NEW.status = 'idle'
         BEGIN SELECT RAISE(ABORT, 'database locked'); END;",
    );
    let executed = Arc::new(Mutex::new(Vec::new()));
    let collector = setup.build(setup.db.store(), &[], ScriptedNotifier::new(None), {
        let executed = executed.clone();
        move |check| {
            locked(&executed).push(check.check_id);
            ok_executor(check)
        }
    });
    let (capture, _guard) = logging::capture();
    collector.collect_once(&Flag::new()).await;
    assert!(
        capture
            .messages()
            .iter()
            .any(|line| line.starts_with("processing-lease reclaim failed\n"))
    );
    assert_eq!(*locked(&executed), [7]);
}

#[tokio::test]
async fn collect_once_records_stale_results_and_still_executes_due_checks() {
    let setup = Setup::new(&[2]);
    setup.db.insert(&wedged(1));
    let notifier = ScriptedNotifier::new(None);
    let collector = setup.build(setup.db.store(), &[], notifier.clone(), ok_executor);
    collector.collect_once(&Flag::new()).await;
    let results = setup.db.results();
    assert_eq!(results.len(), 2);
    // The per-check result is history only: collector-internal, never a page.
    assert_eq!(results[0].0, 1);
    assert_eq!(
        results[0].2,
        "{\"error_type\": \"stale_processing_lease\", \"error_msg\": \"Check processing lease \
         expired; the previous worker did not persist a result\", \"collector_internal\": true}"
    );
    assert_eq!(results[1], (2, "ok".to_owned(), "{}".to_owned()));
    assert_eq!(setup.db.schedule(1), ("idle".to_owned(), T0 + 300, 0));
    assert_eq!(setup.db.state(1), NotificationState::default());
}

#[tokio::test]
async fn a_disabled_reclaimed_check_is_released_without_a_result() {
    let setup = Setup::new(&[]);
    setup.db.insert(&Check {
        disabled: true,
        ..wedged(1)
    });
    setup.db.insert(&wedged(2));
    let notifier = ScriptedNotifier::new(None);
    let collector = setup.build(setup.db.store(), &[], notifier.clone(), ok_executor);
    collector.collect_once(&Flag::new()).await;
    let results = setup.db.results();
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].0, 2);
    assert_eq!(setup.db.schedule(1).0, "idle");
    // One collector-level incident for the batch, never one per member.
    let calls = notifier.calls();
    assert_eq!(calls.len(), 1);
    assert_eq!(
        error_type(&calls[0].1),
        Some("stale_processing_lease_batch")
    );
}

#[tokio::test]
async fn a_stale_result_never_pages_per_check() {
    let setup = Setup::new(&[]);
    setup.db.insert(&wedged(1));
    let notifier = ScriptedNotifier::new(None);
    let collector = setup.build(
        setup.db.store(),
        &[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")],
        notifier.clone(),
        ok_executor,
    );
    let reclaimed = setup
        .db
        .store()
        .reclaim_stale_checks(T0, LEASE_SECONDS, 5)
        .await
        .unwrap();
    assert!(collector.record_stale_check(reclaimed[0].clone()).await);
    assert_eq!(setup.db.results().len(), 1);
    assert_eq!(notifier.count(), 0);
}

fn slow_check(setup: &Setup, check_id: i64, max_runtime: i64) {
    setup.db.insert(&Check {
        data: PyJson::Dict(dict(&format!("{{\"max_runtime_seconds\": {max_runtime}}}"))),
        next_check_time: T0 + 1000,
        ..check(check_id)
    });
}

#[tokio::test]
async fn collect_once_extends_the_lease_and_warns_once() {
    let setup = Setup::new(&[]);
    slow_check(&setup, 1, 90);
    // Claimed 60 s ago: stale for the configured 30 s, not for 90 s.
    setup.db.insert(&Check {
        status: "processing".to_owned(),
        processing_started_at: T0 - 60,
        next_check_time: T0 + 1000,
        ..check(2)
    });
    let collector = setup.build(
        setup.db.store(),
        &[("NYXMON_PROCESSING_LEASE_SECONDS", "30")],
        ScriptedNotifier::new(None),
        ok_executor,
    );
    let (capture, _guard) = logging::capture();
    collector.collect_once(&Flag::new()).await;
    assert_eq!(collector.effective_lease().await, BigInt::from(90));
    let warned = capture
        .messages()
        .iter()
        .filter(|line| {
            *line
                == "configured processing lease 30s is below the estimated maximum check runtime \
                    90s; using 90s"
        })
        .count();
    assert_eq!(warned, 1);
    assert_eq!(
        setup.db.schedule(2),
        ("processing".to_owned(), T0 + 1000, T0 - 60)
    );
}

#[tokio::test]
async fn only_the_derived_lease_is_clamped_and_the_cap_warns_once() {
    let setup = Setup::new(&[]);
    slow_check(&setup, 1, 9_999);
    let collector = setup.build(
        setup.db.store(),
        &[("NYXMON_PROCESSING_LEASE_SECONDS", "30")],
        ScriptedNotifier::new(None),
        ok_executor,
    );
    let (capture, _guard) = logging::capture();
    assert_eq!(collector.effective_lease().await, BigInt::from(3600));
    locked(&collector.lease).refresh_at = None;
    assert_eq!(collector.effective_lease().await, BigInt::from(3600));
    let capped = capture
        .messages()
        .iter()
        .filter(|line| {
            *line
                == "estimated maximum check runtime 3601s exceeds the derived lease ceiling \
                    3600s; clamping the estimate"
        })
        .count();
    assert_eq!(capped, 1);
}

#[tokio::test]
async fn a_failed_estimate_keeps_the_last_safe_budget() {
    let setup = Setup::new(&[]);
    slow_check(&setup, 1, 90);
    let store = Store::open_with(
        &setup.db.path,
        StoreOptions {
            busy_timeout: Duration::from_millis(50),
        },
    )
    .unwrap();
    let collector = setup.build(
        store,
        &[("NYXMON_PROCESSING_LEASE_SECONDS", "30")],
        ScriptedNotifier::new(None),
        ok_executor,
    );
    assert_eq!(collector.effective_lease().await, BigInt::from(90));
    locked(&collector.lease).refresh_at = None;
    let locker = setup.db.raw();
    locker.execute_batch("BEGIN EXCLUSIVE").unwrap();
    let (capture, _guard) = logging::capture();
    assert_eq!(collector.effective_lease().await, BigInt::from(90));
    locker.execute_batch("ROLLBACK").unwrap();
    assert!(capture.messages().iter().any(|line| {
        line.starts_with("processing-lease runtime estimation failed; using the last safe budget\n")
    }));
}

#[tokio::test]
async fn a_lease_sqlite_cannot_bind_fails_the_reclaim() {
    // PLAN.md D-20: `now - lease` below the 64-bit range.
    let setup = Setup::new(&[]);
    setup.db.insert(&Check {
        status: "processing".to_owned(),
        processing_started_at: 0,
        next_check_time: T0 + 1000,
        ..check(1)
    });
    let collector = setup.build(
        setup.db.store(),
        &[("NYXMON_PROCESSING_LEASE_SECONDS", "99999999999999999999")],
        ScriptedNotifier::new(None),
        ok_executor,
    );
    let (capture, _guard) = logging::capture();
    collector.collect_once(&Flag::new()).await;
    assert!(capture.contains(
        "processing-lease reclaim failed\nOverflowError: Python int too large to convert to \
         SQLite INTEGER"
    ));
    // Nothing was written, not even the legacy timestamp.
    assert_eq!(
        setup.db.schedule(1),
        ("processing".to_owned(), T0 + 1000, 0)
    );
}

#[tokio::test]
async fn a_batch_past_its_deadline_is_abandoned_and_execution_rearms_after_it_exits() {
    let setup = Setup::new(&[1]);
    let wedge = Wedge::default();
    let notifier = ScriptedNotifier::new(None);
    let collector = wedged_collector(&setup, notifier.clone(), &wedge);
    let (capture, _guard) = logging::capture();
    for _ in 0..3 {
        collector.collect_once(&Flag::new()).await;
    }
    // Bounded and reported once, at collector level.
    assert_eq!(wedge.calls(), 1);
    assert_eq!(notifier.count(), 1);
    assert_eq!(
        error_type(&notifier.calls()[0].1),
        Some("collector_execution_paused")
    );
    assert!(setup.db.results().is_empty());
    assert!(capture.contains(
        "check batch exceeded its 1000060s execution/result deadline; new executions are paused \
         while lease recovery and alerts continue"
    ));
    assert!(capture.contains("abandoned check batch is still running; new executions are paused"));

    wedge.open();
    wait_abandoned_done(&collector).await;
    // The abandoned batch still records its result.
    assert_eq!(setup.db.results().len(), 1);
    setup.clock.advance(301);
    collector.collect_once(&Flag::new()).await;
    assert_eq!(wedge.calls(), 2);
    assert!(capture.contains(
        "abandoned check batch finished; execution resumed and the collector incident was \
         resolved after 1 alert(s)"
    ));
}

#[tokio::test]
async fn the_paused_alert_never_borrows_an_unrelated_check() {
    let setup = Setup::new(&[1]);
    let wedge = Wedge::default();
    let notifier = ScriptedNotifier::new(None);
    let collector = wedged_collector(&setup, notifier.clone(), &wedge);
    collector.collect_once(&Flag::new()).await;
    setup.db.insert(&check(2));
    collector.collect_once(&Flag::new()).await;
    let calls = notifier.calls();
    assert_eq!(calls.len(), 1);
    let (check, result) = &calls[0];
    assert_eq!(check.check_id, 0);
    assert_eq!(check.name, "Nyxmon collector (execution paused)");
    assert_eq!(result.check_id, 0);
    assert_eq!(error_type(result), Some("collector_execution_paused"));
    // Execution is paused: the due check is neither claimed nor touched.
    assert_eq!(setup.db.schedule(2), ("idle".to_owned(), 0, 0));
    assert!(setup.db.results().is_empty());
    wedge.open();
}

// ------------------------------------------------ test_collector_incidents.py

#[tokio::test]
async fn a_stale_batch_alerts_once_and_recovers_every_check() {
    let setup = Setup::new(&[]);
    let ids: Vec<i64> = (1..=36).collect();
    for id in &ids {
        setup.db.insert(&wedged(*id));
    }
    let notifier = ScriptedNotifier::new(None);
    let collector = setup.build(setup.db.store(), &[], notifier.clone(), ok_executor);
    for _ in 0..10 {
        collector.collect_once(&Flag::new()).await;
    }
    for id in &ids {
        assert_eq!(setup.db.schedule(*id), ("idle".to_owned(), T0 + 300, 0));
        // Lease recovery never touches per-check alert state.
        assert_eq!(setup.db.state(*id), NotificationState::default());
    }
    assert_eq!(setup.db.results().len(), 36);
    let calls = notifier.calls();
    assert_eq!(calls.len(), 1);
    let (incident_check, incident_result) = &calls[0];
    assert_eq!(incident_check.check_id, 0);
    assert_eq!(
        error_type(incident_result),
        Some("stale_processing_lease_batch")
    );
    assert_eq!(
        incident_result.data.get("incident_key"),
        Some(&PyJson::from(STALE_BATCH_INCIDENT_KEY))
    );
    let message = incident_result.data["error_msg"].as_str().unwrap();
    assert!(
        message.starts_with("36 check processing lease(s) expired"),
        "{message}"
    );
    assert!(message.contains("Affected check ids: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10 (+26 more)"));

    // The next real samples dampen normally: 28 OK, 7 errors, 1 warning.
    for id in &ids {
        let status = match id {
            1..=28 => ResultStatus::Ok,
            29..=35 => ResultStatus::Error,
            _ => ResultStatus::Warning,
        };
        let sample = CheckResult::new(*id, status, dict("{\"error_msg\": \"boom\"}"));
        collector
            .handler
            .add_check_result(&check(*id), sample, false)
            .await
            .unwrap();
    }
    assert_eq!(notifier.count(), 1);
}

#[tokio::test]
async fn a_stale_lease_never_pages_even_with_a_first_sample_policy() {
    let setup = Setup::new(&[]);
    let policy = "{\"notification_policy\": {\"consecutive_failures\": 1}}";
    for id in 1..=3 {
        setup.db.insert(&Check {
            data: PyJson::Dict(dict(policy)),
            ..wedged(id)
        });
    }
    let notifier = ScriptedNotifier::new(None);
    let collector = setup.build(setup.db.store(), &[], notifier.clone(), ok_executor);
    collector.collect_once(&Flag::new()).await;
    assert_eq!(notifier.count(), 1);
    assert_eq!(
        error_type(&notifier.calls()[0].1),
        Some("stale_processing_lease_batch")
    );
    // A genuine failure still pages on its first sample.
    let genuine = Check {
        data: PyJson::Dict(dict(policy)),
        ..check(1)
    };
    let sample = CheckResult::new(1, ResultStatus::Error, dict("{\"error_msg\": \"boom\"}"));
    collector
        .handler
        .add_check_result(&genuine, sample, false)
        .await
        .unwrap();
    assert_eq!(notifier.count(), 2);
    assert_eq!(notifier.calls()[1].0.check_id, 1);
}

#[tokio::test]
async fn the_stale_batch_incident_reminds_on_elapsed_time_then_resolves() {
    let setup = Setup::new(&[]);
    setup.db.insert(&wedged(1));
    setup.db.insert(&wedged(2));
    let notifier = ScriptedNotifier::new(None);
    let collector = setup.build(setup.db.store(), &[], notifier.clone(), ok_executor);
    let (capture, _guard) = logging::capture();
    collector.collect_once(&Flag::new()).await;
    assert_eq!(notifier.count(), 1);
    assert!(capture.contains(
        "reclaimed 2 expired check processing lease(s); tracked as a single collector incident \
         (1 alert(s) so far)"
    ));

    // Still stale a minute later: deduplicated.
    restale(&setup, 1);
    setup.clock.advance(60);
    collector.collect_once(&Flag::new()).await;
    assert_eq!(notifier.count(), 1);

    // Past the reminder window: exactly one reminder.
    restale(&setup, 1);
    setup.clock.advance(STALE_BATCH_REMINDER_SECONDS);
    collector.collect_once(&Flag::new()).await;
    assert_eq!(notifier.count(), 2);

    // Quiet for long enough: resolved.
    setup.clock.advance(STALE_BATCH_RESOLVE_AFTER_SECONDS);
    collector.collect_once(&Flag::new()).await;
    assert!(incident(&setup, STALE_BATCH_INCIDENT_KEY).await.is_none());
    assert!(capture.contains(
        "stale processing-lease incident resolved after 4 reclaimed lease(s) and 2 alert(s)"
    ));

    // A genuinely new batch alerts again.
    setup.db.insert(&Check {
        processing_started_at: setup.clock.now() - 10 * LEASE_SECONDS,
        ..wedged(9)
    });
    setup.clock.advance(60);
    collector.collect_once(&Flag::new()).await;
    assert_eq!(notifier.count(), 3);
}

#[tokio::test]
async fn the_stale_batch_incident_is_deduplicated_across_a_restart() {
    let setup = Setup::new(&[]);
    for id in 1..=3 {
        setup.db.insert(&wedged(id));
    }
    let notifier = ScriptedNotifier::new(None);
    let collector = setup.build(setup.db.store(), &[], notifier.clone(), ok_executor);
    collector.collect_once(&Flag::new()).await;
    assert_eq!(notifier.count(), 1);

    // A new process: fresh store, handler and collector on the same file.
    let restarted_notifier = ScriptedNotifier::new(None);
    let restarted = setup.build(
        setup.db.store(),
        &[],
        restarted_notifier.clone(),
        ok_executor,
    );
    for id in 4..=5 {
        setup.db.insert(&wedged(id));
    }
    setup.clock.advance(30);
    restarted.collect_once(&Flag::new()).await;
    assert_eq!(restarted_notifier.count(), 0);
    let incident = incident(&setup, STALE_BATCH_INCIDENT_KEY).await.unwrap();
    assert_eq!(incident.alert_count, 1);
    assert_eq!(
        incident.payload.get("reclaimed_count"),
        Some(&PyJson::Int(5))
    );
}

#[tokio::test]
async fn a_wedged_batch_alerts_once_then_reminds_on_elapsed_time() {
    let setup = Setup::new(&[1]);
    let wedge = Wedge::default();
    let notifier = ScriptedNotifier::new(None);
    let collector = wedged_collector(&setup, notifier.clone(), &wedge);
    collector.collect_once(&Flag::new()).await; // arms the abandoned batch
    collector.collect_once(&Flag::new()).await; // one collector-level alert
    collector.collect_once(&Flag::new()).await; // deduplicated
    setup.clock.advance(60);
    collector.collect_once(&Flag::new()).await; // inside the reminder window
    setup.clock.advance(ABANDONED_BATCH_REMINDER_SECONDS);
    collector.collect_once(&Flag::new()).await; // the reminder

    assert_eq!(wedge.calls(), 1);
    assert!(setup.db.results().is_empty());
    let calls = notifier.calls();
    assert_eq!(calls.len(), 2);
    let (check, result) = &calls[0];
    assert_eq!(check.check_id, 0);
    assert_eq!(result.status, ResultStatus::Error);
    assert_eq!(
        PyJson::Dict(result.data.clone()).to_json_string().unwrap(),
        "{\"error_type\": \"collector_execution_paused\", \"error_msg\": \"All new check \
         executions are paused because an abandoned batch thread is still running after 0s. The \
         wedged batch holds 1 check(s) (check ids: 1). Expired processing leases are still \
         being reclaimed and rescheduled while execution stays paused.\", \"incident_key\": \
         \"collector:execution_paused\", \"incident_alert_count\": 1}"
    );
    let incident = incident(&setup, PAUSED_INCIDENT_KEY).await.unwrap();
    assert_eq!(incident.alert_count, 2);
    assert_eq!(
        PyJson::Dict(incident.payload).to_json_string().unwrap(),
        "{\"incident_type\": \"collector_execution_paused\", \"batch_size\": 1, \"check_ids\": \
         [1]}"
    );
    wedge.open();
}

#[tokio::test]
async fn a_wedged_batch_resolves_and_a_later_wedge_alerts_again() {
    let setup = Setup::new(&[1]);
    let wedge = Wedge::default();
    let notifier = ScriptedNotifier::new(None);
    let collector = wedged_collector(&setup, notifier.clone(), &wedge);
    collector.collect_once(&Flag::new()).await;
    collector.collect_once(&Flag::new()).await;
    assert_eq!(notifier.count(), 1);

    wedge.open();
    wait_abandoned_done(&collector).await;
    collector.collect_once(&Flag::new()).await;
    assert!(incident(&setup, PAUSED_INCIDENT_KEY).await.is_none());

    // A new wedge inside the reminder window of the resolved incident.
    wedge.close();
    setup.clock.advance(301);
    collector.collect_once(&Flag::new()).await;
    collector.collect_once(&Flag::new()).await;
    assert_eq!(notifier.count(), 2);
    wedge.open();
}

#[tokio::test]
async fn a_paused_incident_of_a_previous_process_is_resolved_on_startup() {
    let setup = Setup::new(&[]);
    let mut payload = PyDict::new();
    payload.insert(
        "incident_type".to_owned(),
        PyJson::from("collector_execution_paused"),
    );
    setup
        .db
        .store()
        .claim_collector_incident_alert(
            PAUSED_INCIDENT_KEY,
            T0 - 10,
            ABANDONED_BATCH_REMINDER_SECONDS,
            Some(payload),
        )
        .await
        .unwrap();
    let notifier = ScriptedNotifier::new(None);
    let collector = setup.build(setup.db.store(), &[], notifier.clone(), ok_executor);
    let (capture, _guard) = logging::capture();
    collector.collect_once(&Flag::new()).await;
    assert!(incident(&setup, PAUSED_INCIDENT_KEY).await.is_none());
    assert_eq!(notifier.count(), 0);
    assert!(capture.contains(&format!(
        "collector execution-paused incident opened at {} was resolved on startup after 1 \
         alert(s); the abandoned batch thread did not survive the restart",
        T0 - 10
    )));
    // Only on the first iteration.
    collector.collect_once(&Flag::new()).await;
}

#[tokio::test]
async fn stale_recovery_cannot_release_a_newer_claim() {
    let setup = Setup::new(&[]);
    setup.db.insert(&wedged(1));
    let notifier = ScriptedNotifier::new(None);
    let collector = setup.build(setup.db.store(), &[], notifier.clone(), ok_executor);
    let reclaimed = setup
        .db
        .store()
        .reclaim_stale_checks(T0, LEASE_SECONDS, 5)
        .await
        .unwrap();
    // A fresh worker claims the check before the recovery result lands.
    setup.db.execute(&format!(
        "UPDATE health_check SET status = 'processing', processing_started_at = {T0} WHERE id = 1"
    ));
    assert!(collector.record_stale_check(reclaimed[0].clone()).await);
    assert_eq!(
        setup.db.schedule(1),
        ("processing".to_owned(), T0 + 300, T0)
    );
    assert_eq!(setup.db.results().len(), 1);
    assert_eq!(setup.db.state(1), NotificationState::default());
    assert_eq!(notifier.count(), 0);
}

#[tokio::test]
async fn stale_recovery_does_not_resurrect_a_deleted_check() {
    let setup = Setup::new(&[]);
    setup.db.insert(&wedged(1));
    let notifier = ScriptedNotifier::new(None);
    let collector = setup.build(setup.db.store(), &[], notifier.clone(), ok_executor);
    let reclaimed = setup
        .db
        .store()
        .reclaim_stale_checks(T0, LEASE_SECONDS, 5)
        .await
        .unwrap();
    setup.db.execute("DELETE FROM health_check WHERE id = 1");
    assert!(collector.record_stale_check(reclaimed[0].clone()).await);
    assert!(setup.db.results().is_empty());
    let rows: i64 = setup
        .db
        .raw()
        .query_row("SELECT COUNT(*) FROM health_check", [], |row| row.get(0))
        .unwrap();
    assert_eq!(rows, 0);
    assert_eq!(notifier.count(), 0);
}

fn raising() -> Result<Delivery, NotifyRaised> {
    Err(NotifyRaised {
        class: "RuntimeError",
        message: "telegram unreachable".to_owned(),
    })
}

#[tokio::test]
async fn a_failed_incident_alert_is_retried_and_not_silently_resolved() {
    let setup = Setup::new(&[]);
    setup.db.insert(&wedged(1));
    let notifier = ScriptedNotifier::new(None);
    notifier.set_outcome(|call| {
        if call == 1 {
            raising()
        } else {
            Ok(Delivery::Delivered)
        }
    });
    let collector = setup.build(setup.db.store(), &[], notifier.clone(), ok_executor);
    let (capture, _guard) = logging::capture();
    collector.collect_once(&Flag::new()).await;
    assert_eq!(notifier.count(), 1);
    assert!(retry_pending(&collector, STALE_BATCH_INCIDENT_KEY));
    assert!(capture.messages().iter().any(|line| line
        == "failed to send the collector incident notification for \
            collector:stale_processing_lease\nRuntimeError: telegram unreachable"));

    // Delivery recovers; no new stale leases (a one-off batch).
    setup.clock.advance(ABANDONED_BATCH_RETRY_SECONDS + 1);
    collector.collect_once(&Flag::new()).await;
    assert_eq!(
        notifier.count(),
        2,
        "the failed incident alert was never retried"
    );
    assert!(!retry_pending(&collector, STALE_BATCH_INCIDENT_KEY));

    // Only now, with the alert delivered, may the incident resolve.
    setup.clock.advance(STALE_BATCH_RESOLVE_AFTER_SECONDS + 1);
    collector.collect_once(&Flag::new()).await;
    assert!(incident(&setup, STALE_BATCH_INCIDENT_KEY).await.is_none());
}

/// Panics on its first send, then delivers.
#[derive(Default)]
struct PanicsOnce(AtomicUsize);

impl nyxmon_notify::Notifier for PanicsOnce {
    fn notify_check_failed<'a>(
        &'a self,
        _: &'a Check,
        _: &'a CheckResult,
    ) -> nyxmon_notify::NotifyFuture<'a> {
        Box::pin(async move {
            let call = self.0.fetch_add(1, Ordering::SeqCst) + 1;
            assert!(call != 1, "notifier panics");
            Ok(Delivery::Delivered)
        })
    }
}

#[tokio::test]
async fn a_panicking_notifier_is_a_failed_send_and_the_collector_continues() {
    let setup = Setup::new(&[]);
    setup.db.insert(&wedged(1));
    let notifier = Arc::new(PanicsOnce::default());
    let collector = setup.build(setup.db.store(), &[], notifier.clone(), ok_executor);
    let (capture, _guard) = logging::capture();
    collector.collect_once(&Flag::new()).await;
    assert!(retry_pending(&collector, STALE_BATCH_INCIDENT_KEY));
    assert!(capture.messages().iter().any(|line| line
        == "failed to send the collector incident notification for \
            collector:stale_processing_lease\npanic"));
    let persisted = incident(&setup, STALE_BATCH_INCIDENT_KEY).await.unwrap();
    assert_eq!(
        persisted.payload.get("delivery_pending"),
        Some(&PyJson::Bool(true))
    );

    setup.clock.advance(ABANDONED_BATCH_RETRY_SECONDS + 1);
    collector.collect_once(&Flag::new()).await;
    assert_eq!(notifier.0.load(Ordering::SeqCst), 2);
    assert!(!retry_pending(&collector, STALE_BATCH_INCIDENT_KEY));
}

#[tokio::test]
async fn an_undelivered_incident_is_never_resolved() {
    let setup = Setup::new(&[]);
    setup.db.insert(&wedged(1));
    let notifier = ScriptedNotifier::new(None);
    notifier.set_outcome(|_| raising());
    let collector = setup.build(setup.db.store(), &[], notifier.clone(), ok_executor);
    collector.collect_once(&Flag::new()).await;
    setup.clock.advance(STALE_BATCH_RESOLVE_AFTER_SECONDS * 3);
    collector.collect_once(&Flag::new()).await;
    assert_eq!(notifier.count(), 2);
    assert!(incident(&setup, STALE_BATCH_INCIDENT_KEY).await.is_some());
}

/// `test_failed_delivery_survives_a_worker_restart` and
/// `test_a_crash_between_claim_and_send_retries_after_a_restart`.
#[tokio::test]
async fn a_failed_delivery_survives_a_restart() {
    let setup = Setup::new(&[]);
    setup.db.insert(&wedged(1));
    let notifier = ScriptedNotifier::new(None);
    notifier.set_outcome(|_| raising());
    let collector = setup.build(setup.db.store(), &[], notifier.clone(), ok_executor);
    collector.collect_once(&Flag::new()).await;
    assert_eq!(notifier.count(), 1);
    let persisted = incident(&setup, STALE_BATCH_INCIDENT_KEY).await.unwrap();
    assert_eq!(
        persisted.payload.get("delivery_pending"),
        Some(&PyJson::Bool(true))
    );
    assert_eq!(
        persisted.payload.get("delivery_attempt"),
        Some(&PyJson::Int(persisted.alert_count))
    );

    let restarted_notifier = ScriptedNotifier::new(None);
    let restarted = setup.build(
        setup.db.store(),
        &[],
        restarted_notifier.clone(),
        ok_executor,
    );
    assert!(!retry_pending(&restarted, STALE_BATCH_INCIDENT_KEY));
    setup.clock.advance(ABANDONED_BATCH_RETRY_SECONDS + 1);
    restarted.collect_once(&Flag::new()).await;
    assert_eq!(restarted_notifier.count(), 1);
    let reloaded = incident(&setup, STALE_BATCH_INCIDENT_KEY).await.unwrap();
    assert!(reloaded.payload.get("delivery_pending").is_none());
}

#[tokio::test]
async fn a_failed_send_marks_the_stale_batch_undelivered() {
    let setup = Setup::new(&[]);
    setup.db.insert(&wedged(1));
    let notifier = ScriptedNotifier::new(None);
    notifier.always(Delivery::Failed);
    let collector = setup.build(setup.db.store(), &[], notifier.clone(), ok_executor);
    collector.collect_once(&Flag::new()).await;
    assert_eq!(notifier.count(), 1);
    assert!(retry_pending(&collector, STALE_BATCH_INCIDENT_KEY));
    let persisted = incident(&setup, STALE_BATCH_INCIDENT_KEY).await.unwrap();
    assert_eq!(
        persisted.payload.get("delivery_pending"),
        Some(&PyJson::Bool(true))
    );
    assert_eq!(
        persisted.payload.get("delivery_attempt"),
        Some(&PyJson::Int(1))
    );

    notifier.always(Delivery::Delivered);
    setup.clock.advance(ABANDONED_BATCH_RETRY_SECONDS + 1);
    collector.collect_once(&Flag::new()).await;
    assert_eq!(notifier.count(), 2);
    let cleared = incident(&setup, STALE_BATCH_INCIDENT_KEY).await.unwrap();
    assert!(cleared.payload.get("delivery_pending").is_none());
    assert!(cleared.payload.get("delivery_attempt").is_none());
}

#[tokio::test]
async fn a_notifier_that_attempted_nothing_starts_no_retry_loop() {
    let setup = Setup::new(&[]);
    setup.db.insert(&wedged(1));
    let notifier = ScriptedNotifier::new(None);
    notifier.always(Delivery::Unknown);
    let collector = setup.build(setup.db.store(), &[], notifier.clone(), ok_executor);
    collector.collect_once(&Flag::new()).await;
    let persisted = incident(&setup, STALE_BATCH_INCIDENT_KEY).await.unwrap();
    assert_eq!(persisted.alert_count, 1);
    assert!(!retry_pending(&collector, STALE_BATCH_INCIDENT_KEY));
    assert!(persisted.payload.get("delivery_pending").is_none());
    assert!(persisted.payload.get("delivery_attempt").is_none());

    setup.clock.advance(ABANDONED_BATCH_RETRY_SECONDS + 1);
    collector.collect_once(&Flag::new()).await;
    let unchanged = incident(&setup, STALE_BATCH_INCIDENT_KEY).await.unwrap();
    assert_eq!(unchanged.alert_count, 1);
}

#[tokio::test]
async fn a_failed_send_marks_the_paused_batch_undelivered() {
    let setup = Setup::new(&[1]);
    let wedge = Wedge::default();
    let notifier = ScriptedNotifier::new(None);
    notifier.always(Delivery::Failed);
    let collector = wedged_collector(&setup, notifier.clone(), &wedge);
    collector.collect_once(&Flag::new()).await;
    collector.collect_once(&Flag::new()).await;
    assert!(retry_pending(&collector, PAUSED_INCIDENT_KEY));
    let persisted = incident(&setup, PAUSED_INCIDENT_KEY).await.unwrap();
    assert_eq!(
        persisted.payload.get("delivery_pending"),
        Some(&PyJson::Bool(true))
    );

    // The failed send is retried after a minute instead of an hour.
    notifier.always(Delivery::Delivered);
    setup.clock.advance(ABANDONED_BATCH_RETRY_SECONDS + 1);
    collector.collect_once(&Flag::new()).await;
    assert_eq!(notifier.count(), 2);
    assert!(!retry_pending(&collector, PAUSED_INCIDENT_KEY));
    wedge.open();
}

#[tokio::test]
async fn a_non_granting_payload_refresh_keeps_the_retry_intent() {
    let setup = Setup::new(&[]);
    setup.db.insert(&wedged(1));
    let notifier = ScriptedNotifier::new(None);
    notifier.always(Delivery::Failed);
    let collector = setup.build(setup.db.store(), &[], notifier.clone(), ok_executor);
    collector.collect_once(&Flag::new()).await;
    assert_eq!(notifier.count(), 1);

    // A second expired lease inside the retry window: no alert granted.
    setup.db.insert(&wedged(2));
    setup.clock.advance(5);
    collector.collect_once(&Flag::new()).await;
    assert_eq!(notifier.count(), 1);
    let refreshed = incident(&setup, STALE_BATCH_INCIDENT_KEY).await.unwrap();
    assert_eq!(
        refreshed.payload.get("reclaimed_count"),
        Some(&PyJson::Int(2))
    );
    assert_eq!(
        refreshed.payload.get("delivery_pending"),
        Some(&PyJson::Bool(true))
    );

    // After a restart the persisted intent makes the retry happen.
    let restarted_notifier = ScriptedNotifier::new(None);
    let restarted = setup.build(
        setup.db.store(),
        &[],
        restarted_notifier.clone(),
        ok_executor,
    );
    setup.clock.advance(ABANDONED_BATCH_RETRY_SECONDS + 1);
    restarted.collect_once(&Flag::new()).await;
    assert_eq!(restarted_notifier.count(), 1);
    let cleared = incident(&setup, STALE_BATCH_INCIDENT_KEY).await.unwrap();
    assert!(cleared.payload.get("delivery_pending").is_none());
}

#[tokio::test]
async fn a_late_acknowledgement_cannot_clear_a_newer_intent() {
    let setup = Setup::new(&[]);
    setup.db.insert(&wedged(1));
    let collector = setup.build(
        setup.db.store(),
        &[],
        ScriptedNotifier::new(None),
        ok_executor,
    );
    collector.collect_once(&Flag::new()).await;
    let current = incident(&setup, STALE_BATCH_INCIDENT_KEY).await.unwrap();
    let mut payload = current.payload.clone();
    payload.insert("delivery_pending".to_owned(), PyJson::Bool(true));
    payload.insert("delivery_attempt".to_owned(), PyJson::Int(7));
    setup
        .db
        .store()
        .set_collector_incident_payload(STALE_BATCH_INCIDENT_KEY, payload)
        .await
        .unwrap();

    collector
        .record_incident_send(STALE_BATCH_INCIDENT_KEY, true, 3)
        .await;

    let after = incident(&setup, STALE_BATCH_INCIDENT_KEY).await.unwrap();
    assert_eq!(
        after.payload.get("delivery_pending"),
        Some(&PyJson::Bool(true))
    );
    assert_eq!(after.payload.get("delivery_attempt"), Some(&PyJson::Int(7)));
}

// --------------------------------------------------- test_shutdown_claims.py

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn shutdown_during_stale_recovery_keeps_results_and_alert() {
    let setup = Setup::new(&[]);
    setup.db.insert(&wedged(1));
    setup.db.insert(&wedged(2));
    let recording = Arc::new(Flag::new());
    let stopping = Arc::new(Flag::new());
    let gate: RecoveryGate = {
        let recording = recording.clone();
        let stopping = stopping.clone();
        Arc::new(move || {
            let recording = recording.clone();
            let stopping = stopping.clone();
            Box::pin(async move {
                // Stop arrives after the reclaim committed, while the first
                // result is being recorded.
                recording.set();
                stopping.wait().await;
                tokio::time::sleep(Duration::from_millis(200)).await;
            })
        })
    };
    let notifier = ScriptedNotifier::new(None);
    let collector = Arc::new(
        setup
            .build(
                setup.db.store(),
                &[("NYXMON_SHUTDOWN_GRACE_SECONDS", "0")],
                notifier.clone(),
                ok_executor,
            )
            .with_recovery_gate(gate),
    );
    let run = tokio::spawn(collector.clone().run());
    recording.wait().await;
    stopping.set();
    stop_and_join(&collector, run).await;

    let mut recorded: Vec<i64> = setup.db.results().iter().map(|row| row.0).collect();
    recorded.sort_unstable();
    assert_eq!(recorded, [1, 2]);
    assert!(
        setup
            .db
            .results()
            .iter()
            .all(|row| row.2.contains("\"error_type\": \"stale_processing_lease\""))
    );
    let calls = notifier.calls();
    assert_eq!(calls.len(), 1);
    assert_eq!(
        error_type(&calls[0].1),
        Some("stale_processing_lease_batch")
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn shutdown_gives_an_abandoned_batch_the_grace_then_releases_its_claims() {
    let setup = Setup::new(&[1]);
    let wedge = Wedge::default();
    let notifier = ScriptedNotifier::new(None);
    let collector = Arc::new(
        setup
            .build(
                setup.db.store(),
                &[LONG_LEASE, ("NYXMON_SHUTDOWN_GRACE_SECONDS", "0.1")],
                notifier,
                wedge.executor(),
            )
            .with_batch_deadline(Duration::from_millis(50)),
    );
    let run = tokio::spawn(collector.clone().run());
    until(|| collector.abandoned_running()).await;
    assert!(collector.active().is_empty());
    let elapsed = stop_and_join(&collector, run).await;
    assert!(elapsed >= Duration::from_millis(100), "{elapsed:?}");
    assert!(setup.db.results().is_empty());
    assert_eq!(setup.db.schedule(1), ("idle".to_owned(), T0 + 300, 0));
}

/// Like Python, the collector loop keeps sleeping its interval after the
/// stop, so the shutdown deadline runs its full course (grace, intake
/// stop, flush window) even when the abandoned batch finished meanwhile.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_abandoned_batch_finishing_within_the_grace_is_recorded() {
    let setup = Setup::new(&[1]);
    let wedge = Wedge::default();
    let collector = Arc::new(
        setup
            .build(
                setup.db.store(),
                &[LONG_LEASE, ("NYXMON_SHUTDOWN_GRACE_SECONDS", "0.3")],
                ScriptedNotifier::new(None),
                wedge.executor(),
            )
            .with_batch_deadline(Duration::from_millis(50))
            .with_flush_timeout(Duration::from_millis(200)),
    );
    let run = tokio::spawn(collector.clone().run());
    until(|| collector.abandoned_running()).await;
    let opener = {
        let wedge = wedge.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(100)).await;
            wedge.open();
        })
    };
    let elapsed = stop_and_join(&collector, run).await;
    opener.await.unwrap();
    assert!(elapsed >= Duration::from_millis(500), "{elapsed:?}");
    assert_eq!(setup.db.results(), [(1, "ok".to_owned(), "{}".to_owned())]);
    assert_eq!(setup.db.schedule(1), ("idle".to_owned(), T0 + 300, 0));
}
