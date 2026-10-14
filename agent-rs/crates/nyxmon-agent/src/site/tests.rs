//! Runtime parts of `tests/unit/test_site_connectivity_observer.py`: the
//! loop, the default probe runner, the probe round budget, the store and
//! notifier wiring and the snapshot the result handler reads.

use std::collections::HashSet;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use nyxmon_core::env::{ProbeKind, ProbeTarget, SiteEnvConfig, SiteMode};
use nyxmon_core::log::{RecordingSink, WarnOnce};
use nyxmon_core::model::ResultStatus;
use nyxmon_core::pyjson::PyJson;
use nyxmon_core::site::observer::{SITE_INCIDENT_KEY, SITE_SUMMARY_ERROR_TYPE};
use nyxmon_core::site::{PathName, PathState, PortError};
use nyxmon_notify::{Delivery, NotifyRaised};
use nyxmon_store::Store;

use super::*;
use crate::logging;
use crate::testutil::{ManualClock, ScriptedNotifier, TestDb, check};

pub(super) const T0: i64 = 1_700_000_000;
pub(super) const PROBE_INTERVAL: i64 = 60;

pub(super) fn target(path: &'static str, host: &str, port: u16) -> ProbeTarget {
    ProbeTarget {
        path,
        kind: if path == "dns" {
            ProbeKind::Dns
        } else {
            ProbeKind::Tcp
        },
        host: host.to_owned(),
        port,
    }
}

/// `make_config` of the Python tests (enforce, two failures to go down).
pub(super) fn make_config() -> SiteEnvConfig {
    SiteEnvConfig {
        mode: SiteMode::Enforce,
        probe_interval: PROBE_INTERVAL,
        probe_timeout: 3,
        ipv4_targets: vec![
            target("ipv4", "1.1.1.1", 443),
            target("ipv4", "8.8.8.8", 443),
        ],
        ipv6_targets: vec![target("ipv6", "2606:4700:4700::1111", 443)],
        dns_names: vec![target("dns", "cloudflare.com", 0)],
        down_after_failures: 2,
        recovery_grace_seconds: 900,
        max_hold_seconds: 10800,
        incident_notify_after_seconds: 900,
        incident_reminder_seconds: 21600,
    }
}

type ProbeHook = Box<dyn Fn(usize) + Send + Sync>;

/// `ScriptedProbeRunner`: fails whole paths, or single targets, on demand.
#[derive(Default)]
pub(super) struct ScriptedProbes {
    pub down_paths: Mutex<HashSet<&'static str>>,
    pub down_targets: Mutex<HashSet<String>>,
    pub calls: AtomicUsize,
    /// Called with the number of probes made so far (tests of the loop).
    pub hook: Mutex<Option<ProbeHook>>,
}

impl ScriptedProbes {
    pub fn set_down(&self, paths: &[&'static str]) {
        *self.down_paths.lock().unwrap() = paths.iter().copied().collect();
    }
}

impl ProbeRunner for ScriptedProbes {
    fn probe<'a>(&'a self, target: &'a ProbeTarget) -> ProbeFuture<'a> {
        let calls = self.calls.fetch_add(1, Ordering::SeqCst) + 1;
        if let Some(hook) = &*self.hook.lock().unwrap() {
            hook(calls);
        }
        let answered = !self
            .down_targets
            .lock()
            .unwrap()
            .contains(&target.to_string())
            && !self.down_paths.lock().unwrap().contains(target.path);
        Box::pin(async move { Ok(answered) })
    }
}

fn warnings() -> (Arc<WarnOnce>, Arc<RecordingSink>) {
    let sink = Arc::new(RecordingSink::default());
    (Arc::new(WarnOnce::new(sink.clone())), sink)
}

fn runtime(
    config: SiteEnvConfig,
    store: Store,
    clock: &ManualClock,
    probes: Arc<dyn ProbeRunner>,
    notifier: Option<Arc<ScriptedNotifier>>,
) -> SiteRuntime {
    let notifier = notifier.map(|notifier| notifier as Arc<dyn Notifier>);
    SiteRuntime::with_probes(config, warnings().0, store, notifier, clock.clock(), probes)
}

/// The persisted payload of the `site:connectivity` row.
async fn payload(store: &Store) -> PyDict {
    store
        .get_collector_incident(SITE_INCIDENT_KEY)
        .await
        .unwrap()
        .expect("the site row exists")
        .payload
}

fn int(payload: &PyDict, key: &str) -> i64 {
    match payload.get(key) {
        Some(PyJson::Int(value)) => *value,
        other => panic!("{key}: {other:?}"),
    }
}

fn text<'a>(payload: &'a PyDict, key: &str) -> &'a str {
    match payload.get(key) {
        Some(PyJson::Str(value)) => value,
        other => panic!("{key}: {other:?}"),
    }
}

#[tokio::test(start_paused = true)]
async fn run_restores_then_loops_until_stopped() {
    let db = TestDb::new();
    let store = db.store();
    // The observer's clock follows tokio's paused clock: each sleep of one
    // probe interval moves it on by exactly that much.
    let start = tokio::time::Instant::now();
    let clock: Clock = Arc::new(move || T0 + 60 + start.elapsed().as_secs() as i64);
    let probes = Arc::new(ScriptedProbes::default());
    probes.set_down(&["ipv4"]);
    let stop = Arc::new(Flag::new());
    let per_round = make_config().all_targets().len();
    {
        let stop = stop.clone();
        *probes.hook.lock().unwrap() = Some(Box::new(move |calls| {
            if calls == 3 * per_round {
                stop.set();
            }
        }));
    }
    let site = SiteRuntime::with_probes(
        make_config(),
        warnings().0,
        store.clone(),
        None,
        clock,
        probes.clone(),
    );
    site.run(stop).await;

    assert_eq!(probes.calls.load(Ordering::SeqCst), 3 * per_round);
    let payload = payload(&store).await;
    assert_eq!(text(&payload, "phase"), "active");
    assert_eq!(int(&payload, "incident_id"), T0 + 120);
    assert_eq!(int(&payload, "observed_at"), T0 + 180);
}

#[tokio::test(flavor = "current_thread", start_paused = true)]
async fn run_logs_and_continues_when_a_round_raises() {
    let (capture, _guard) = logging::capture();
    let db = TestDb::new();
    let store = db.store();
    // A summary record whose `attempt_at` Python's `int()` cannot convert:
    // every round raises in `_process_summaries` after its write.
    store
        .open_collector_incident(
            SITE_INCIDENT_KEY,
            T0,
            crate::testutil::dict(
                r#"{"incident_type": "site_connectivity", "version": 1, "phase": "idle",
                    "incident_id": 0, "observed_at": 0, "paths": {},
                    "ongoing": {}, "summaries": [{"incident_id": 5, "started_at": 5,
                    "attempt_at": Infinity}], "recheck": {}}"#,
            ),
        )
        .await
        .unwrap();
    let clock = ManualClock::new(T0);
    let probes = Arc::new(ScriptedProbes::default());
    let stop = Arc::new(Flag::new());
    let per_round = make_config().all_targets().len();
    {
        let stop = stop.clone();
        *probes.hook.lock().unwrap() = Some(Box::new(move |calls| {
            if calls == 2 * per_round {
                stop.set();
            }
        }));
    }
    let site = runtime(make_config(), store, &clock, probes.clone(), None);
    site.run(stop).await;

    assert_eq!(probes.calls.load(Ordering::SeqCst), 2 * per_round);
    let failed: Vec<String> = capture
        .messages()
        .into_iter()
        .filter(|message| message.starts_with("site connectivity round failed"))
        .collect();
    assert_eq!(
        failed,
        vec!["site connectivity round failed\nOverflowError"; 2]
    );
}

#[tokio::test(flavor = "current_thread", start_paused = true)]
async fn a_restore_that_raises_stops_the_observer_with_a_log_line() {
    let (capture, _guard) = logging::capture();
    let db = TestDb::new();
    let store = db.store();
    store
        .open_collector_incident(
            SITE_INCIDENT_KEY,
            T0,
            crate::testutil::dict(r#"{"version": 1, "observed_at": -Infinity}"#),
        )
        .await
        .unwrap();
    let clock = ManualClock::new(T0);
    let probes = Arc::new(ScriptedProbes::default());
    let site = runtime(make_config(), store, &clock, probes.clone(), None);
    site.run(Arc::new(Flag::new())).await;

    assert_eq!(probes.calls.load(Ordering::SeqCst), 0);
    assert!(capture.contains(
        "site connectivity observer stopped: restoring the persisted lifecycle failed\n\
         OverflowError"
    ));
}

#[tokio::test]
async fn run_returns_immediately_when_the_feature_is_off() {
    let db = TestDb::new();
    let store = db.store();
    let clock = ManualClock::new(T0);
    let probes = Arc::new(ScriptedProbes::default());
    let config = SiteEnvConfig {
        mode: SiteMode::Off,
        ..make_config()
    };
    let site = runtime(config, store.clone(), &clock, probes.clone(), None);
    site.run(Arc::new(Flag::new())).await;
    assert_eq!(probes.calls.load(Ordering::SeqCst), 0);
    assert!(
        store
            .get_collector_incident(SITE_INCIDENT_KEY)
            .await
            .unwrap()
            .is_none()
    );
}

#[tokio::test(flavor = "current_thread")]
async fn default_probe_runner_reports_a_refused_connection() {
    let (capture, _guard) = logging::capture();
    let runner = DefaultProbeRunner::new(1);
    let refused = target("ipv4", "127.0.0.1", 1);
    assert!(!runner.probe(&refused).await.unwrap());
    assert_eq!(refused.to_string(), "127.0.0.1:1");
    assert!(capture.lines().iter().any(|line| {
        line.level == "DEBUG"
            && line.logger == "nyxmon.adapters.site_connectivity"
            && line
                .message
                .starts_with("site connectivity probe failed for 127.0.0.1:1\n")
    }));

    // An open port answers; the connection is closed again at once.
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    assert!(
        runner
            .probe(&target("ipv4", "127.0.0.1", port))
            .await
            .unwrap()
    );

    // A name that resolves answers; one that cannot be encoded fails.
    assert!(runner.probe(&target("dns", "localhost", 0)).await.unwrap());
    assert!(!runner.probe(&target("dns", "a..b", 0)).await.unwrap());
}

#[tokio::test(start_paused = true)]
async fn a_tcp_probe_is_bounded_by_the_per_target_timeout() {
    // A listener whose backlog is full never completes a connect; a
    // non-routable address does the same. Either way the timeout decides.
    let runner = DefaultProbeRunner::new(1);
    let unroutable = target("ipv4", "10.255.255.1", 9);
    let probe = runner.probe(&unroutable);
    let outcome = tokio::time::timeout(Duration::from_secs(5), probe)
        .await
        .expect("bounded by the per-target timeout");
    assert!(!outcome.unwrap());
}

/// A probe that never finishes, or raises.
struct StuckProbes {
    raise_for: &'static str,
}

impl ProbeRunner for StuckProbes {
    fn probe<'a>(&'a self, target: &'a ProbeTarget) -> ProbeFuture<'a> {
        Box::pin(async move {
            if target.path == self.raise_for {
                return Err(PortError(
                    "RuntimeError: probe subsystem is on fire".to_owned(),
                ));
            }
            if target.path == "dns" {
                return Ok(true);
            }
            std::future::pending().await
        })
    }
}

#[tokio::test(flavor = "current_thread", start_paused = true)]
async fn unfinished_targets_count_as_failed_after_the_round_budget() {
    let (capture, _guard) = logging::capture();
    let db = TestDb::new();
    let clock = ManualClock::new(T0);
    let mut site = runtime(
        make_config(),
        db.store(),
        &clock,
        Arc::new(StuckProbes { raise_for: "ipv6" }),
        None,
    );
    let started = tokio::time::Instant::now();
    site.run_once().await.unwrap();
    // probe_timeout (3) + PROBE_ROUND_SLACK_SECONDS (2).
    assert_eq!(started.elapsed(), Duration::from_secs(5));
    let snapshot = site.snapshot();
    assert_eq!(
        snapshot.path(PathName::Ipv4).unwrap().state,
        PathState::Failing
    );
    assert_eq!(
        snapshot.path(PathName::Ipv6).unwrap().state,
        PathState::Failing
    );
    assert_eq!(snapshot.path(PathName::Dns).unwrap().state, PathState::Up);
    assert!(capture.lines().iter().any(|line| line.level == "ERROR"
        && line.message
            == "site connectivity probe raised for [2606:4700:4700::1111]:443\n\
                RuntimeError: probe subsystem is on fire"));
}

#[tokio::test]
async fn lifecycle_against_the_real_store() {
    let db = TestDb::new();
    let store = db.store();
    let clock = ManualClock::new(T0);
    let probes = Arc::new(ScriptedProbes::default());
    probes.set_down(&["ipv4"]);
    let notifier = ScriptedNotifier::new(None);
    let mut site = runtime(
        make_config(),
        store.clone(),
        &clock,
        probes.clone(),
        Some(notifier.clone()),
    );
    for at in [T0 + 60, T0 + 120] {
        clock.advance(at - clock.now());
        site.run_once().await.unwrap();
    }
    let persisted = payload(&store).await;
    assert_eq!(text(&persisted, "phase"), "active");
    assert_eq!(int(&persisted, "incident_id"), T0 + 120);

    // A fresh observer restores the row through the same store.
    let mut restored = runtime(make_config(), store.clone(), &clock, probes.clone(), None);
    restored.restore().await.unwrap();
    let snapshot = restored.snapshot();
    assert_eq!(
        snapshot.path(PathName::Ipv4).unwrap().state,
        PathState::Down
    );
    assert_eq!(snapshot.incident_id, T0 + 120);

    // Held checks are counted from the store for the summary record
    // (`test_held_checks_are_counted_without_the_blocking_portal`).
    for check_id in [1, 2, 3, 4] {
        db.insert(&check(check_id));
        db.execute(&format!(
            "INSERT INTO check_notification_state (check_id, failure_count, last_attempt_count,
                last_immediate_at, last_notified_at, first_failure_at, held_since, attempt_seq,
                attempt_at) VALUES ({check_id}, 1, 0, 0, 0, {T0}, {T0}, 0, 0)"
        ));
    }
    probes.set_down(&[]);
    for at in [T0 + 1140, T0 + 2040] {
        clock.advance(at - clock.now());
        site.run_once().await.unwrap();
    }
    let summaries: Vec<String> = notifier
        .calls()
        .into_iter()
        .filter(|(_, result)| {
            result.data.get("error_type") == Some(&PyJson::Str(SITE_SUMMARY_ERROR_TYPE.to_owned()))
        })
        .map(|(_, result)| match result.data.get("error_msg") {
            Some(PyJson::Str(message)) => message.clone(),
            other => panic!("{other:?}"),
        })
        .collect();
    assert_eq!(summaries.len(), 1);
    assert!(
        summaries[0].contains("4 dependent check(s) were held"),
        "{summaries:?}"
    );
    // Delivered: the record is gone from the persisted payload.
    assert_eq!(
        payload(&store).await.get("summaries"),
        Some(&PyJson::List(Vec::new()))
    );
}

#[tokio::test(flavor = "current_thread")]
async fn site_messages_go_through_the_process_notifier_as_the_synthetic_row() {
    let (capture, _guard) = logging::capture();
    let db = TestDb::new();
    let clock = ManualClock::new(T0);
    let notifier = ScriptedNotifier::new(None);
    let site = runtime(
        make_config(),
        db.store(),
        &clock,
        Arc::new(ScriptedProbes::default()),
        Some(notifier.clone()),
    );
    let message = SiteMessage {
        incident_key: SITE_INCIDENT_KEY,
        name: "site connectivity",
        error_type: SITE_SUMMARY_ERROR_TYPE,
        error_msg: "Site connectivity outage over".to_owned(),
        status: "warning",
        opsgate_ticket: false,
    };
    let ports = &site.ports;

    // Only a reported failure is undelivered; "cannot tell" is delivered.
    for (delivery, expected) in [
        (Delivery::Delivered, SiteDelivery::Delivered),
        (Delivery::Unknown, SiteDelivery::Delivered),
        (Delivery::Failed, SiteDelivery::Failed),
    ] {
        notifier.always(delivery);
        assert_eq!(ports.notify(&message).await, Ok(expected));
    }
    let (row, result) = notifier.calls().pop().unwrap();
    assert_eq!((row.check_id, row.name.as_str()), (0, "site connectivity"));
    assert_eq!(row.url, "internal://collector");
    assert_eq!(result.status, ResultStatus::Warning);
    assert_eq!(
        PyJson::Dict(result.data).to_json_string().unwrap(),
        r#"{"error_type": "site_connectivity_summary", "error_msg": "Site connectivity outage over", "incident_key": "site:connectivity", "incident_alert_count": 0, "opsgate_ticket": false}"#
    );

    // A raising notifier is logged by the adapter and counts as failed.
    notifier.set_outcome(|_| {
        Err(NotifyRaised {
            class: "OverflowError",
            message: "date value out of range".to_owned(),
        })
    });
    assert_eq!(ports.notify(&message).await, Ok(SiteDelivery::Failed));
    assert!(capture.lines().iter().any(|line| line.level == "ERROR"
        && line.logger == "nyxmon.adapters.collector"
        && line.message
            == "failed to send the site connectivity notification for site:connectivity\n\
                OverflowError: date value out of range"));

    // Without a notifier every send fails with the documented line.
    let unconfigured = runtime(
        make_config(),
        db.store(),
        &clock,
        Arc::new(ScriptedProbes::default()),
        None,
    );
    assert_eq!(
        unconfigured.ports.notify(&message).await,
        Ok(SiteDelivery::Failed)
    );
    assert!(capture.lines().iter().any(|line| line.level == "ERROR"
        && line.logger == "nyxmon.adapters.site_connectivity"
        && line.message == "the site connectivity notifier is not configured"));
}

#[tokio::test]
async fn the_handler_reads_every_published_snapshot() {
    let db = TestDb::new();
    let clock = ManualClock::new(T0);
    let probes = Arc::new(ScriptedProbes::default());
    probes.set_down(&["dns"]);
    let config = SiteEnvConfig {
        max_hold_seconds: 1234,
        ..make_config()
    };
    let mut site = runtime(config, db.store(), &clock, probes, None);
    let state = site.state();
    // The construction snapshot: nothing observed yet.
    assert_eq!(state.snapshot().unwrap().observed_at, 0);
    assert_eq!(state.limits().max_hold, 1234);
    assert_eq!(state.limits().stale_after, 3 * PROBE_INTERVAL);

    site.run_once().await.unwrap();
    clock.advance(PROBE_INTERVAL);
    site.run_once().await.unwrap();
    let snapshot = state.snapshot().unwrap();
    assert_eq!(snapshot.observed_at, T0 + PROBE_INTERVAL);
    assert_eq!(snapshot.path(PathName::Dns).unwrap().state, PathState::Down);
    assert_eq!(snapshot, site.snapshot());
}

#[tokio::test]
async fn unobserved_requirements_are_reported_once_per_check() {
    let db = TestDb::new();
    let mut ipv6_only = check(1);
    ipv6_only.data = PyJson::Dict(crate::testutil::dict(
        r#"{"site_dependency": {"requires": ["ipv6"]}}"#,
    ));
    db.insert(&ipv6_only);
    let mut internet = check(2);
    internet.data = PyJson::Dict(crate::testutil::dict(r#"{"site_dependency": "internet"}"#));
    db.insert(&internet);

    let (warnings, sink) = warnings();
    let clock = ManualClock::new(T0);
    let config = SiteEnvConfig {
        ipv6_targets: Vec::new(),
        ..make_config()
    };
    let site = SiteRuntime::with_probes(
        config,
        warnings,
        db.store(),
        None,
        clock.clock(),
        Arc::new(ScriptedProbes::default()),
    );
    site.warn_unobserved_site_requirements().await;
    site.warn_unobserved_site_requirements().await;
    let lines = sink.take();
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert!(
        lines[0].1.contains("requires the unobserved path(s) ipv6"),
        "{lines:?}"
    );
}
