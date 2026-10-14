//! `tests/unit/test_site_connectivity_holds.py`: end-to-end site holds
//! through the production wiring (the observer runtime over the SQLite store,
//! its snapshot injected into the result handler, one notifier for check
//! alerts and site messages), driven by a manual clock and scripted probes.

use std::collections::HashMap;
use std::sync::Arc;

use nyxmon_core::env::{SiteEnvConfig, SiteMode};
use nyxmon_core::log::{RecordingSink, WarnOnce};
use nyxmon_core::model::{Check, CheckResult, NotificationState, ResultStatus};
use nyxmon_core::pyjson::{PyDict, PyJson, loads};
use nyxmon_core::site::observer::{
    SITE_INCIDENT_KEY, SITE_ONGOING_ERROR_TYPE, SITE_SUMMARY_ERROR_TYPE,
};
use nyxmon_core::site::{PathName, PathSnapshot, PathState};
use nyxmon_notify::Delivery;
use nyxmon_store::{PersistRequest, Store};

use super::tests::{PROBE_INTERVAL, ScriptedProbes, T0, make_config};
use super::*;
use crate::results::ResultHandler;
use crate::testutil::{ManualClock, ScriptedNotifier, TestDb, check, dict, env};

const NOTIFY_AFTER: i64 = 900;
const STALE_AFTER: i64 = 3 * PROBE_INTERVAL;
const RETRY: i64 = 300;
const DATA_KEY: &str = "site_connectivity";

const ERROR: ResultStatus = ResultStatus::Error;
const OK: ResultStatus = ResultStatus::Ok;

/// A wired handler and observer over one database (`Harness`).
struct Harness {
    db: Arc<TestDb>,
    store: Store,
    clock: ManualClock,
    notifier: Arc<ScriptedNotifier>,
    probes: Arc<ScriptedProbes>,
    site: Option<SiteRuntime>,
    handler: ResultHandler<Store>,
    sink: Arc<RecordingSink>,
    checks: HashMap<i64, Check>,
}

struct Build<'a> {
    db: Option<Arc<TestDb>>,
    clock: Option<ManualClock>,
    vars: &'a [(&'a str, &'a str)],
    config: SiteEnvConfig,
}

impl Default for Build<'_> {
    fn default() -> Self {
        Build {
            db: None,
            clock: None,
            vars: &[],
            config: make_config(),
        }
    }
}

/// `bootstrap` as the binary does it (`app.rs`).
fn build(spec: Build<'_>) -> Harness {
    let db = spec.db.unwrap_or_else(|| Arc::new(TestDb::new()));
    let store = db.store();
    let clock = spec.clock.unwrap_or_else(|| ManualClock::new(T0));
    let mut vars = vec![("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "86400")];
    vars.extend_from_slice(spec.vars);
    let (env, sink) = env(&vars);
    let warnings = Arc::new(WarnOnce::new(sink.clone()));
    let notifier = ScriptedNotifier::new(None);
    let probes = Arc::new(ScriptedProbes::default());
    let site = (spec.config.mode != SiteMode::Off).then(|| {
        SiteRuntime::with_probes(
            spec.config,
            warnings.clone(),
            store.clone(),
            Some(notifier.clone()),
            clock.clock(),
            probes.clone(),
        )
    });
    let mut handler = ResultHandler::new(store.clone(), notifier.clone(), env, warnings)
        .with_clock(clock.clock())
        .with_suppression_fetch(Arc::new(|_| Box::pin(async { None })));
    if let Some(site) = &site {
        handler = handler.with_site_state(site.state());
    }
    Harness {
        db,
        store,
        clock,
        notifier,
        probes,
        site,
        handler,
        sink,
        checks: HashMap::new(),
    }
}

fn data(json: &str) -> PyJson {
    loads(json).unwrap()
}

impl Harness {
    fn now(&self) -> i64 {
        self.clock.now()
    }

    fn move_to(&self, epoch: i64) {
        self.clock.advance(epoch - self.clock.now());
    }

    fn add_check(&mut self, check_id: i64, check_data: &str, interval: i64, due_in: i64) {
        let mut row = check(check_id);
        row.name = format!("check-{check_id}");
        row.check_interval = interval;
        row.next_check_time = self.now() + due_in;
        row.data = data(check_data);
        self.db.insert(&row);
        self.checks.insert(check_id, row);
    }

    /// Mark the check as processing, as the collector's claim would.
    fn claim(&mut self, check_id: i64, at: Option<i64>) -> i64 {
        let at = at.unwrap_or_else(|| self.now());
        self.db.execute(&format!(
            "UPDATE health_check SET status = 'processing', processing_started_at = {at}
             WHERE id = {check_id}"
        ));
        at
    }

    /// Claim the check, run it, and hand the result to the handler; returns
    /// the stored result data.
    async fn submit_claimed(
        &mut self,
        check_id: i64,
        status: ResultStatus,
        claimed_at: Option<i64>,
    ) -> PyDict {
        let claimed_at = self.claim(check_id, claimed_at);
        let mut executing = self.checks[&check_id].clone();
        executing.status = "idle".to_owned();
        executing.processing_started_at = 0;
        executing.next_check_time = self.now() + executing.check_interval;
        executing.claim_started_at = claimed_at;
        self.handler
            .add_check_result(
                &executing,
                CheckResult::new(check_id, status, PyDict::new()),
                false,
            )
            .await
            .unwrap();
        self.checks.get_mut(&check_id).unwrap().next_check_time = executing.next_check_time;
        let (stored_id, _, stored) = self.db.results().pop().unwrap();
        assert_eq!(stored_id, check_id);
        dict(&stored)
    }

    async fn submit(&mut self, check_id: i64, status: ResultStatus) -> PyDict {
        self.submit_claimed(check_id, status, None).await
    }

    fn state(&self, check_id: i64) -> NotificationState {
        self.db.state(check_id)
    }

    fn next_check_time(&self, check_id: i64) -> i64 {
        self.db.schedule(check_id).1
    }

    /// One probe round with the given paths failing.
    async fn round(&mut self, down: Option<&[&'static str]>, advance: i64) {
        self.clock.advance(advance);
        if let Some(down) = down {
            self.probes.set_down(down);
        }
        self.site.as_mut().unwrap().run_once().await.unwrap();
    }

    async fn rounds(&mut self, count: usize, down: Option<&[&'static str]>) {
        for index in 0..count {
            let advance = if index == 0 { 0 } else { PROBE_INTERVAL };
            self.round(down, advance).await;
        }
    }

    /// Advance one probe interval at a time, probing as we go.
    async fn elapse(&mut self, seconds: i64, down: Option<&[&'static str]>) {
        let mut remaining = seconds;
        while remaining > 0 {
            let step = remaining.min(PROBE_INTERVAL);
            remaining -= step;
            self.round(down, step).await;
        }
    }

    fn path(&self, name: PathName) -> PathSnapshot {
        *self.site.as_ref().unwrap().snapshot().path(name).unwrap()
    }

    /// Two failed rounds confirm a path down; returns `down_since`.
    async fn take_down(&mut self, name: PathName, label: &'static str) -> i64 {
        self.round(Some(&[label]), 0).await;
        self.round(Some(&[label]), PROBE_INTERVAL).await;
        assert_eq!(self.path(name).state, PathState::Down);
        self.path(name).down_since
    }

    fn alerts_for(&self, check_id: i64) -> usize {
        self.notifier
            .calls()
            .iter()
            .filter(|(row, _)| row.check_id == check_id)
            .count()
    }

    fn check_alerts(&self) -> usize {
        self.notifier
            .calls()
            .iter()
            .filter(|(row, _)| row.check_id != 0)
            .count()
    }

    fn site_messages(&self, error_type: &str) -> Vec<CheckResult> {
        self.notifier
            .calls()
            .into_iter()
            .filter(|(row, result)| {
                row.check_id == 0
                    && result.data.get("error_type") == Some(&PyJson::Str(error_type.to_owned()))
            })
            .map(|(_, result)| result)
            .collect()
    }

    fn deliver(&self, delivered: bool) {
        self.notifier.always(if delivered {
            Delivery::Delivered
        } else {
            Delivery::Failed
        });
    }
}

fn meta(result: &PyDict) -> &PyDict {
    result
        .get(DATA_KEY)
        .and_then(PyJson::as_dict)
        .expect("site metadata")
}

static NULL: PyJson = PyJson::Null;

fn field<'a>(result: &'a PyDict, key: &str) -> &'a PyJson {
    meta(result).get(key).unwrap_or(&NULL)
}

fn s(value: &str) -> PyJson {
    PyJson::Str(value.to_owned())
}

const INTERNET: &str = r#"{"site_dependency": "internet"}"#;
const NONE: &str = "{}";

#[tokio::test]
async fn short_reconnect_produces_no_alert_at_all() {
    let mut h = build(Build::default());
    h.add_check(1, INTERNET, 300, 300);

    // A first failure while everything is still up: below threshold.
    h.submit(1, ERROR).await;
    assert_eq!(h.state(1).failure_count, 1);
    assert_eq!(h.state(1).held_since, 0);

    let down_since = h.take_down(PathName::Dns, "dns").await;

    // The next failing sample would reach the threshold, but it is held.
    h.clock.advance(30);
    let result = h.submit(1, ERROR).await;
    assert_eq!(field(&result, "held"), &PyJson::Bool(true));
    assert_eq!(field(&result, "reason"), &s("dependency_down"));
    assert_eq!(field(&result, "paths"), &data(r#"["dns"]"#));
    assert_eq!(field(&result, "down_since"), &PyJson::Int(down_since));
    assert_eq!(h.state(1).held_since, h.now());
    assert_eq!(h.state(1).failure_count, 2);
    assert_eq!(h.notifier.count(), 0);

    // Reconnect, then the full grace.
    h.round(Some(&[]), PROBE_INTERVAL).await;
    assert_eq!(h.path(PathName::Dns).state, PathState::Recovering);
    h.move_to(h.path(PathName::Dns).release_at);
    h.round(Some(&[]), 0).await;
    assert_eq!(h.path(PathName::Dns).state, PathState::Up);

    // The rechecked sample is healthy: the incident clears silently.
    h.submit(1, OK).await;
    let state = h.state(1);
    assert_eq!(
        state,
        NotificationState {
            attempt_seq: state.attempt_seq,
            ..NotificationState::default()
        }
    );
    assert_eq!(h.notifier.count(), 0);
}

#[tokio::test]
async fn one_failing_probe_target_holds_nothing() {
    let mut h = build(Build {
        vars: &[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")],
        ..Build::default()
    });
    h.add_check(
        1,
        r#"{"site_dependency": {"requires": ["ipv4"]}}"#,
        300,
        300,
    );
    h.probes
        .down_targets
        .lock()
        .unwrap()
        .insert("1.1.1.1:443".to_owned());

    h.rounds(3, None).await;
    assert_eq!(h.path(PathName::Ipv4).state, PathState::Up);

    let result = h.submit(1, ERROR).await;
    assert!(!result.contains_key(DATA_KEY));
    assert_eq!(h.alerts_for(1), 1);
}

#[tokio::test]
async fn long_outage_yields_one_summary_and_one_genuine_alert() {
    let mut h = build(Build::default());
    h.add_check(1, INTERNET, 300, 300);
    h.add_check(2, INTERNET, 300, 300);

    // Telegram is unreachable through the same broken path.
    h.deliver(false);
    let down_since = h.take_down(PathName::Dns, "dns").await;

    // Both dependants fail repeatedly while the path is down: all held.
    for _ in 0..4 {
        h.elapse(300, Some(&["dns"])).await;
        h.submit(1, ERROR).await;
        h.submit(2, ERROR).await;
    }
    assert_eq!(h.check_alerts(), 0);
    assert_eq!(h.state(1).failure_count, 4);
    assert!(h.state(2).held_since > 0);

    // 15 minutes in, the observer attempted the ongoing alert; it failed.
    assert!(h.now() - down_since >= NOTIFY_AFTER);
    let ongoing_attempts = h.site_messages(SITE_ONGOING_ERROR_TYPE).len();
    assert!(ongoing_attempts >= 1);

    // Reconnect: the undelivered ongoing alert is retired, never sent late.
    h.deliver(true);
    h.round(Some(&[]), PROBE_INTERVAL).await;
    assert_eq!(h.path(PathName::Dns).state, PathState::Recovering);
    h.elapse(180, Some(&[])).await;
    assert_eq!(
        h.site_messages(SITE_ONGOING_ERROR_TYPE).len(),
        ongoing_attempts,
        "an obsolete outage alert was delivered after reconnect"
    );

    // Release: the outage closes and exactly one summary is sent.
    h.move_to(h.path(PathName::Dns).release_at);
    h.round(Some(&[]), 0).await;
    let summaries = h.site_messages(SITE_SUMMARY_ERROR_TYPE);
    assert_eq!(summaries.len(), 1);
    assert_eq!(
        summaries[0].data.get("opsgate_ticket"),
        Some(&PyJson::Bool(false))
    );
    assert_eq!(summaries[0].status, ResultStatus::Warning);

    // Further rounds must not repeat it.
    h.rounds(2, None).await;
    assert_eq!(h.site_messages(SITE_SUMMARY_ERROR_TYPE).len(), 1);

    // The recovered service goes quiet, the broken one alerts exactly once.
    h.clock.advance(10);
    h.submit(2, OK).await;
    h.submit(1, ERROR).await;
    assert_eq!(h.alerts_for(2), 0);
    assert_eq!(h.alerts_for(1), 1);
    assert_eq!(h.state(1).held_since, 0);

    h.clock.advance(300);
    h.submit(1, ERROR).await;
    assert_eq!(h.alerts_for(1), 1);
}

#[tokio::test]
async fn flap_during_the_grace_keeps_holding() {
    let mut h = build(Build::default());
    h.add_check(1, INTERNET, 300, 300);
    h.take_down(PathName::Dns, "dns").await;

    h.round(Some(&[]), PROBE_INTERVAL).await;
    assert_eq!(h.path(PathName::Dns).state, PathState::Recovering);
    let first_release = h.path(PathName::Dns).release_at;

    // A sample measured during the grace is still held.
    let result = h.submit(1, ERROR).await;
    assert_eq!(field(&result, "reason"), &s("dependency_recovering"));

    // A failed round returns the path to `down` without re-confirmation.
    h.round(Some(&["dns"]), PROBE_INTERVAL).await;
    assert_eq!(h.path(PathName::Dns).state, PathState::Down);

    // The old release time has passed, but the grace restarted.
    h.move_to(first_release + 1);
    h.round(Some(&["dns"]), 0).await;
    let result = h.submit(1, ERROR).await;
    assert_eq!(field(&result, "held"), &PyJson::Bool(true));
    assert_eq!(h.check_alerts(), 0);

    h.round(Some(&[]), PROBE_INTERVAL).await;
    assert!(h.path(PathName::Dns).release_at > first_release);
}

#[tokio::test]
async fn ipv6_only_outage_holds_ipv6_checks_but_not_internet() {
    let mut h = build(Build {
        vars: &[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")],
        ..Build::default()
    });
    h.add_check(1, INTERNET, 300, 300);
    h.add_check(
        2,
        r#"{"site_dependency": {"requires": ["ipv6"]}}"#,
        300,
        300,
    );
    h.take_down(PathName::Ipv6, "ipv6").await;

    let internet = h.submit(1, ERROR).await;
    let ipv6_only = h.submit(2, ERROR).await;

    assert!(!internet.contains_key(DATA_KEY));
    assert_eq!(h.alerts_for(1), 1);
    assert_eq!(field(&ipv6_only, "held"), &PyJson::Bool(true));
    assert_eq!(field(&ipv6_only, "paths"), &data(r#"["ipv6"]"#));
    assert_eq!(h.alerts_for(2), 0);
}

#[tokio::test]
async fn dns_outage_holds_internet_checks() {
    let mut h = build(Build {
        vars: &[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")],
        ..Build::default()
    });
    h.add_check(1, INTERNET, 300, 300);
    h.take_down(PathName::Dns, "dns").await;

    let result = h.submit(1, ERROR).await;
    assert_eq!(field(&result, "paths"), &data(r#"["dns"]"#));
    assert_eq!(h.check_alerts(), 0);
}

#[tokio::test]
async fn ipv4_only_site_holds_internet_checks_on_an_ipv4_outage() {
    let mut h = build(Build {
        vars: &[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")],
        config: SiteEnvConfig {
            ipv6_targets: Vec::new(),
            ..make_config()
        },
        ..Build::default()
    });
    h.add_check(1, INTERNET, 300, 300);
    assert_eq!(h.path(PathName::Ipv6).state, PathState::Unobserved);

    // A single failed round only makes the path `failing`: nothing held.
    h.round(Some(&["ipv4"]), 0).await;
    assert_eq!(h.path(PathName::Ipv4).state, PathState::Failing);
    let failing_sample = h.submit(1, ERROR).await;
    assert!(!failing_sample.contains_key(DATA_KEY));
    assert_eq!(h.alerts_for(1), 1);

    // Confirmed down: the any-of group is unmet and the check is held.
    h.round(Some(&["ipv4"]), PROBE_INTERVAL).await;
    assert_eq!(h.path(PathName::Ipv4).state, PathState::Down);
    let held = h.submit(1, ERROR).await;
    assert_eq!(field(&held, "held"), &PyJson::Bool(true));
    assert_eq!(field(&held, "paths"), &data(r#"["ipv4"]"#));
    assert_eq!(h.alerts_for(1), 1);
}

#[tokio::test]
async fn a_requirement_on_an_unobserved_path_never_holds_and_warns_once() {
    let mut h = build(Build {
        vars: &[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")],
        config: SiteEnvConfig {
            ipv6_targets: Vec::new(),
            ..make_config()
        },
        ..Build::default()
    });
    h.add_check(
        1,
        r#"{"site_dependency": {"requires": ["ipv6"]}}"#,
        300,
        10_000,
    );

    let site = h.site.as_ref().unwrap();
    site.warn_unobserved_site_requirements().await;
    site.warn_unobserved_site_requirements().await;
    let warned = h
        .sink
        .take()
        .into_iter()
        .filter(|(_, message)| message.contains("requires the unobserved path(s) ipv6"))
        .count();
    assert_eq!(warned, 1);

    h.rounds(3, Some(&["ipv4", "dns"])).await;
    let result = h.submit(1, ERROR).await;
    assert!(!result.contains_key(DATA_KEY));
    assert_eq!(h.alerts_for(1), 1);
}

#[tokio::test]
async fn malformed_site_dependency_warns_once_and_is_unclassified() {
    let mut h = build(Build {
        vars: &[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")],
        ..Build::default()
    });
    h.add_check(
        1,
        r#"{"site_dependency": {"requires": ["wifi"]}}"#,
        300,
        300,
    );
    h.take_down(PathName::Dns, "dns").await;
    h.sink.take();

    let first = h.submit(1, ERROR).await;
    h.clock.advance(300);
    let second = h.submit(1, ERROR).await;

    assert!(!first.contains_key(DATA_KEY));
    assert!(!second.contains_key(DATA_KEY));
    let warned = h
        .sink
        .take()
        .into_iter()
        .filter(|(_, message)| message.contains("site_dependency for check_id=1 is invalid"))
        .count();
    assert_eq!(warned, 1);
    assert_eq!(h.alerts_for(1), 1);
}

#[tokio::test]
async fn local_failure_during_an_outage_alerts_and_retries_delivery() {
    let mut h = build(Build {
        vars: &[
            ("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1"),
            ("NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS", "300"),
        ],
        ..Build::default()
    });
    h.add_check(1, NONE, 300, 300);
    h.take_down(PathName::Dns, "dns").await;

    // Telegram is unreachable during the full outage.
    h.deliver(false);
    h.submit(1, ERROR).await;
    assert_eq!(h.alerts_for(1), 1);
    assert_eq!(h.state(1).attempt_at, h.now());

    // Below the retry interval: no repeat.
    h.clock.advance(RETRY - 1);
    h.submit(1, ERROR).await;
    assert_eq!(h.alerts_for(1), 1);

    // Every retry interval one further attempt, all failing.
    for _ in 0..2 {
        h.clock.advance(RETRY);
        h.submit(1, ERROR).await;
    }
    assert_eq!(h.alerts_for(1), 3);
    assert_eq!(h.state(1).attempt_at, h.now());

    // After the reconnect the still-failing check finally delivers.
    h.deliver(true);
    h.clock.advance(RETRY);
    h.submit(1, ERROR).await;
    assert_eq!(h.alerts_for(1), 4);
    assert_eq!(h.state(1).attempt_at, 0);

    // Nothing repeats after the acknowledged send.
    h.clock.advance(RETRY * 4);
    h.submit(1, ERROR).await;
    assert_eq!(h.alerts_for(1), 4);
}

#[tokio::test]
async fn a_local_failure_that_recovered_is_never_reported_late() {
    let mut h = build(Build {
        vars: &[
            ("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1"),
            ("NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS", "300"),
        ],
        ..Build::default()
    });
    h.add_check(1, NONE, 300, 300);
    h.deliver(false);

    h.submit(1, ERROR).await;
    h.deliver(true);
    h.clock.advance(RETRY + 1);
    h.submit(1, OK).await;

    h.clock.advance(RETRY * 5);
    assert_eq!(h.alerts_for(1), 1);
    assert_eq!(h.state(1).attempt_at, 0);
}

#[tokio::test]
async fn the_retry_knob_at_zero_keeps_todays_behaviour() {
    let mut h = build(Build {
        vars: &[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")],
        ..Build::default()
    });
    h.add_check(1, NONE, 300, 300);
    h.deliver(false);

    h.submit(1, ERROR).await;
    for _ in 0..5 {
        h.clock.advance(RETRY * 2);
        h.submit(1, ERROR).await;
    }
    assert_eq!(h.alerts_for(1), 1);
}

#[tokio::test]
async fn a_sample_measured_before_the_release_is_held_and_rechecked() {
    let mut h = build(Build {
        vars: &[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")],
        ..Build::default()
    });
    h.add_check(1, INTERNET, 300, 3000);
    h.take_down(PathName::Dns, "dns").await;

    // The check is claimed during the grace and is still running at release.
    h.round(Some(&[]), PROBE_INTERVAL).await;
    let claimed_at = h.claim(1, None);
    let release_at = h.path(PathName::Dns).release_at;

    // The release happens while that claim is still in flight.
    h.move_to(release_at);
    h.round(Some(&[]), 0).await;
    assert_eq!(h.path(PathName::Dns).last_release_at, release_at);

    // Only afterwards is the buffered failing result handled.
    h.clock.advance(120);
    let result = h.submit_claimed(1, ERROR, Some(claimed_at)).await;
    assert_eq!(field(&result, "held"), &PyJson::Bool(true));
    assert_eq!(field(&result, "reason"), &s("measured_before_release"));
    assert_eq!(field(&result, "release_at"), &PyJson::Int(release_at));
    assert_eq!(h.check_alerts(), 0);

    // The next observer round pulls the held, idle check forward.
    h.round(Some(&[]), PROBE_INTERVAL).await;
    assert_eq!(h.next_check_time(1), h.now());

    // Its fresh sample decides: it re-arms the budget and alerts.
    h.clock.advance(10);
    let fresh = h.submit(1, ERROR).await;
    assert!(!fresh.contains_key(DATA_KEY));
    assert_eq!(h.state(1).held_since, 0);
    assert_eq!(h.alerts_for(1), 1);
}

#[tokio::test]
async fn held_samples_count_towards_the_checks_own_threshold() {
    let mut h = build(Build::default());
    h.add_check(
        1,
        r#"{"site_dependency": "internet", "notification_policy": {"consecutive_failures": 4}}"#,
        3600,
        300,
    );
    h.add_check(
        2,
        r#"{"site_dependency": "internet", "notification_policy": {"consecutive_failures": 2}}"#,
        300,
        300,
    );
    h.take_down(PathName::Dns, "dns").await;

    // One held sample for the hourly check, two for the five-minute one.
    h.submit(1, ERROR).await;
    h.submit(2, ERROR).await;
    h.elapse(300, Some(&["dns"])).await;
    h.submit(2, ERROR).await;
    assert_eq!(h.check_alerts(), 0);

    // Reconnect and release.
    h.round(Some(&[]), PROBE_INTERVAL).await;
    h.move_to(h.path(PathName::Dns).release_at);
    h.round(Some(&[]), 0).await;

    // The five-minute check already met its threshold: it alerts at once.
    h.clock.advance(10);
    h.submit(2, ERROR).await;
    assert_eq!(h.alerts_for(2), 1);

    // The hourly check needs three more samples, as without the outage.
    for _ in 0..2 {
        h.clock.advance(3600);
        h.submit(1, ERROR).await;
    }
    assert_eq!(h.alerts_for(1), 0);
    h.clock.advance(3600);
    h.submit(1, ERROR).await;
    assert_eq!(h.alerts_for(1), 1);
}

#[tokio::test]
async fn a_check_blocked_by_another_requirement_is_not_rescheduled() {
    let mut h = build(Build::default());
    h.add_check(1, INTERNET, 3600, 3600);
    h.add_check(
        2,
        r#"{"site_dependency": {"requires": ["ipv4"]}}"#,
        3600,
        3600,
    );

    h.rounds(2, Some(&["ipv4", "dns"])).await;
    h.submit(1, ERROR).await;
    h.submit(2, ERROR).await;
    assert!(h.state(1).held_since > 0);
    assert!(h.state(2).held_since > 0);

    // ipv4 comes back and is released; dns is still down.
    h.round(Some(&["dns"]), PROBE_INTERVAL).await;
    h.move_to(h.path(PathName::Ipv4).release_at);
    h.round(Some(&["dns"]), 0).await;

    assert_eq!(h.next_check_time(2), h.now());
    assert!(h.next_check_time(1) > h.now());
}

#[tokio::test]
async fn a_stale_snapshot_stops_holding_without_touching_held_since() {
    let mut h = build(Build {
        vars: &[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")],
        ..Build::default()
    });
    h.add_check(1, INTERNET, 300, 300);
    h.take_down(PathName::Dns, "dns").await;

    h.submit(1, ERROR).await;
    let held_since = h.state(1).held_since;
    assert!(held_since > 0);
    assert_eq!(h.check_alerts(), 0);

    // The observer freezes: no further rounds, the snapshot goes stale.
    h.clock.advance(STALE_AFTER + 1);
    let result = h.submit(1, ERROR).await;
    assert!(!result.contains_key(DATA_KEY));
    assert_eq!(h.alerts_for(1), 1);
    assert_eq!(
        h.state(1).held_since,
        held_since,
        "a stale bypass must not re-arm the hold budget"
    );
}

#[tokio::test]
async fn the_hold_is_bounded_by_max_hold_seconds() {
    let mut h = build(Build {
        vars: &[
            ("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1"),
            ("NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS", "300"),
        ],
        config: SiteEnvConfig {
            max_hold_seconds: 1800,
            ..make_config()
        },
        ..Build::default()
    });
    h.add_check(1, INTERNET, 300, 300);
    h.take_down(PathName::Dns, "dns").await;

    h.submit(1, ERROR).await;
    let held_since = h.state(1).held_since;
    assert_eq!(h.check_alerts(), 0);

    // Still inside the budget, still down: held.
    h.clock.advance(1700);
    h.round(Some(&["dns"]), 0).await;
    h.submit(1, ERROR).await;
    assert_eq!(h.check_alerts(), 0);

    // The budget is spent: ordinary policy applies although still down.
    h.clock.advance(200);
    h.round(Some(&["dns"]), 0).await;
    h.deliver(false);
    let exhausted = h.submit(1, ERROR).await;
    assert_eq!(field(&exhausted, "held"), &PyJson::Bool(false));
    assert_eq!(field(&exhausted, "exhausted"), &PyJson::Bool(true));
    assert_eq!(h.alerts_for(1), 1);
    assert_eq!(
        h.state(1).held_since,
        held_since,
        "the budget must not re-arm"
    );

    // The delivery retry after exhaustion is not held either.
    h.deliver(true);
    h.clock.advance(RETRY);
    h.round(Some(&["dns"]), 0).await;
    h.submit(1, ERROR).await;
    assert_eq!(h.alerts_for(1), 2);
    assert_eq!(h.state(1).held_since, held_since);
}

#[tokio::test]
async fn observe_mode_records_metadata_without_changing_decisions() {
    let mut h = build(Build {
        vars: &[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")],
        config: SiteEnvConfig {
            mode: SiteMode::Observe,
            ..make_config()
        },
        ..Build::default()
    });
    h.add_check(1, INTERNET, 300, 300);
    h.take_down(PathName::Dns, "dns").await;

    let result = h.submit(1, ERROR).await;
    assert_eq!(field(&result, "held"), &PyJson::Bool(false));
    assert_eq!(field(&result, "reason"), &s("dependency_down"));
    assert_eq!(h.alerts_for(1), 1);
    assert_eq!(h.state(1).held_since, 0);
}

#[tokio::test]
async fn mode_off_starts_no_observer_and_writes_no_metadata() {
    let mut h = build(Build {
        vars: &[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")],
        config: SiteEnvConfig {
            mode: SiteMode::Off,
            ..make_config()
        },
        ..Build::default()
    });
    assert!(h.site.is_none());

    h.add_check(1, INTERNET, 300, 300);
    let result = h.submit(1, ERROR).await;
    assert!(!result.contains_key(DATA_KEY));
    assert_eq!(h.alerts_for(1), 1);
    assert!(
        h.store
            .get_collector_incident(SITE_INCIDENT_KEY)
            .await
            .unwrap()
            .is_none()
    );
}

/// A new process over the same database (`build(store=SqliteStore(path))`).
fn restart(first: &Harness) -> Harness {
    let mut second = build(Build {
        db: Some(first.db.clone()),
        clock: Some(first.clock.clone()),
        vars: &[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")],
        ..Build::default()
    });
    second.checks = first.checks.clone();
    second
}

#[tokio::test]
async fn a_restart_during_the_grace_keeps_holding() {
    let mut first = build(Build {
        vars: &[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")],
        ..Build::default()
    });
    first.add_check(1, INTERNET, 300, 300);
    first.take_down(PathName::Dns, "dns").await;
    first.submit(1, ERROR).await;
    assert!(first.state(1).held_since > 0);

    first.round(Some(&[]), PROBE_INTERVAL).await;
    let release_at = first.path(PathName::Dns).release_at;
    assert_eq!(first.path(PathName::Dns).state, PathState::Recovering);

    // --- restart: a brand new wiring over the same file ---
    let mut second = restart(&first);
    second.site.as_mut().unwrap().restore().await.unwrap();
    assert_eq!(second.path(PathName::Dns).state, PathState::Recovering);
    assert_eq!(second.path(PathName::Dns).release_at, release_at);

    // The grace is neither restarted nor released early: still held.
    second.clock.advance(30);
    let result = second.submit(1, ERROR).await;
    assert_eq!(field(&result, "held"), &PyJson::Bool(true));
    assert_eq!(second.check_alerts(), 0);

    // After the persisted release the fresh sample decides on its own.
    second.move_to(release_at);
    second.round(Some(&[]), 0).await;
    second.clock.advance(10);
    let fresh = second.submit(1, ERROR).await;
    assert!(!fresh.contains_key(DATA_KEY));
    assert_eq!(second.alerts_for(1), 1);
}

#[tokio::test]
async fn a_restart_during_an_incident_restores_down_and_keeps_holding() {
    let mut first = build(Build {
        vars: &[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")],
        ..Build::default()
    });
    first.add_check(1, INTERNET, 300, 300);
    let down_since = first.take_down(PathName::Dns, "dns").await;

    let mut second = restart(&first);
    second.site.as_mut().unwrap().restore().await.unwrap();
    assert_eq!(second.path(PathName::Dns).state, PathState::Down);
    assert_eq!(second.path(PathName::Dns).down_since, down_since);

    second.clock.advance(30);
    let result = second.submit(1, ERROR).await;
    assert_eq!(field(&result, "held"), &PyJson::Bool(true));
    assert_eq!(second.check_alerts(), 0);
}

#[tokio::test]
async fn a_staggered_release_holds_a_buffered_sample_across_a_restart() {
    let mut first = build(Build {
        vars: &[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")],
        ..Build::default()
    });
    first.add_check(1, INTERNET, 300, 3000);

    first.rounds(2, Some(&["ipv4", "ipv6"])).await;
    assert_eq!(first.path(PathName::Ipv4).state, PathState::Down);
    assert_eq!(first.path(PathName::Ipv6).state, PathState::Down);

    // IPv4 comes back and serves its grace; IPv6 stays down throughout.
    first.round(Some(&["ipv6"]), PROBE_INTERVAL).await;
    assert_eq!(first.path(PathName::Ipv4).state, PathState::Recovering);
    let release_at = first.path(PathName::Ipv4).release_at;
    let remaining = release_at - first.now() - PROBE_INTERVAL;
    first.elapse(remaining, Some(&["ipv6"])).await;

    // The check is claimed while nothing carries the group ...
    let claimed_at = first.claim(1, None);
    assert!(claimed_at < release_at);

    // ... and the IPv4 release happens while that claim is in flight.
    first.move_to(release_at);
    first.round(Some(&["ipv6"]), 0).await;
    assert_eq!(first.path(PathName::Ipv4).state, PathState::Up);
    assert_eq!(first.path(PathName::Ipv4).last_release_at, release_at);
    assert_eq!(first.path(PathName::Ipv6).state, PathState::Down);

    first.clock.advance(120);
    let result = first.submit_claimed(1, ERROR, Some(claimed_at)).await;
    assert_eq!(field(&result, "held"), &PyJson::Bool(true));
    assert_eq!(field(&result, "reason"), &s("measured_before_release"));
    assert_eq!(field(&result, "paths"), &data(r#"["ipv4"]"#));
    assert_eq!(field(&result, "release_at"), &PyJson::Int(release_at));
    assert!(first.state(1).held_since > 0);
    assert_eq!(first.check_alerts(), 0);

    // --- restart: a brand new wiring over the same file ---
    let mut second = restart(&first);
    second.site.as_mut().unwrap().restore().await.unwrap();
    assert_eq!(second.path(PathName::Ipv4).last_release_at, release_at);
    assert_eq!(second.path(PathName::Ipv6).state, PathState::Down);

    // The restored watermark still holds another buffered sample.
    second.clock.advance(30);
    let buffered = second.submit_claimed(1, ERROR, Some(claimed_at)).await;
    assert_eq!(field(&buffered, "held"), &PyJson::Bool(true));
    assert!(second.state(1).held_since > 0);
    assert_eq!(second.check_alerts(), 0);

    // A sample claimed after the release decides on its own merits.
    second.clock.advance(10);
    let fresh = second.submit(1, ERROR).await;
    assert!(!fresh.contains_key(DATA_KEY));
    assert_eq!(second.state(1).held_since, 0);
    assert_eq!(second.alerts_for(1), 1);
}

#[tokio::test]
async fn a_stale_claim_marks_no_hold() {
    let mut h = build(Build {
        vars: &[("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")],
        ..Build::default()
    });
    h.add_check(1, INTERNET, 300, 300);
    h.take_down(PathName::Dns, "dns").await;

    // A newer worker holds the claim; the old result arrives afterwards.
    h.claim(1, None);
    let mut stale = h.checks[&1].clone();
    stale.claim_started_at = h.now() - 5000;
    let persisted = h
        .store
        .persist_check_result(PersistRequest {
            check: stale,
            result: CheckResult::new(1, ERROR, PyDict::new()),
            transition: None,
            complete_check: true,
            hold_marker: Some(h.now()),
        })
        .await
        .unwrap();
    assert!(!persisted);
    assert_eq!(h.store.count_held_checks().await.unwrap(), 0);
}
