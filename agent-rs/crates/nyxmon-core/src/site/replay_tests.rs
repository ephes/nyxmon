//! Replay of `fixtures/site_replay/*.json` (PLAN.md section 7.2): the
//! Python observer's call sequence, payload bytes, snapshots, messages and
//! log lines, round by round, against the same scripted fakes.

use std::future::Future;
use std::pin::pin;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll, Waker};

use super::observer::{
    LogLevel, Observer, PortError, ProbeOutcome, SiteDelivery, SiteMessage, SitePorts,
};
use super::render::Raised;
use super::snapshot::SiteSnapshot;
use crate::env::{ProbeKind, ProbeTarget, SiteEnvConfig, SiteMode};
use crate::fixture::{Field, load};
use crate::log::{LogSink, WarnOnce};
use crate::model::HeldCheck;
use crate::pyjson::{DumpOpts, PyDict, PyJson, dumps, loads};

const FIXTURES: [(&str, &str); 7] = [
    (
        "state_machine",
        include_str!("../../../../fixtures/site_replay/state_machine.json"),
    ),
    (
        "ongoing",
        include_str!("../../../../fixtures/site_replay/ongoing.json"),
    ),
    (
        "summaries",
        include_str!("../../../../fixtures/site_replay/summaries.json"),
    ),
    (
        "restarts",
        include_str!("../../../../fixtures/site_replay/restarts.json"),
    ),
    (
        "store_failures",
        include_str!("../../../../fixtures/site_replay/store_failures.json"),
    ),
    (
        "recheck",
        include_str!("../../../../fixtures/site_replay/recheck.json"),
    ),
    (
        "restore_payloads",
        include_str!("../../../../fixtures/site_replay/restore_payloads.json"),
    ),
];

/// Drive a future whose ports never pend (every fake answers at once).
fn block_on<F: Future>(future: F) -> F::Output {
    let mut future = pin!(future);
    let mut context = Context::from_waker(Waker::noop());
    match future.as_mut().poll(&mut context) {
        Poll::Ready(output) => output,
        Poll::Pending => panic!("a fake port pended"),
    }
}

fn text(value: &str) -> PyJson {
    PyJson::from(value)
}

fn list(items: impl IntoIterator<Item = PyJson>) -> PyJson {
    PyJson::List(items.into_iter().collect())
}

fn payload_text(payload: &PyDict) -> String {
    dumps(&PyJson::Dict(payload.clone()), &DumpOpts::python_default()).expect("dumpable")
}

/// The persistent fake state of `gen_site.py:_World`.
#[derive(Default)]
struct State {
    now: i64,
    row: Option<PyDict>,
    down_paths: Vec<String>,
    down_targets: Vec<String>,
    raise_targets: Vec<String>,
    fail_reads: bool,
    fail_writes: bool,
    deliver: String,
    held: Vec<HeldCheck>,
    in_flight: i64,
    held_at_close: Option<i64>,
    fail_in_flight: bool,
    fail_held: bool,
    fail_reschedule: bool,
    fail_count_held: bool,
    fail_write_calls: Vec<i64>,
    deliver_calls: Vec<String>,
    write_calls: i64,
    probed: Vec<String>,
}

#[derive(Default)]
struct World {
    state: Mutex<State>,
    events: Arc<Mutex<Vec<PyJson>>>,
}

/// Routes `WarnOnce` lines into the shared event list.
struct EventSink(Arc<Mutex<Vec<PyJson>>>);

impl LogSink for EventSink {
    fn warning(&self, logger: &'static str, message: &str) {
        self.0.lock().unwrap().push(list([
            text("log"),
            text("WARNING"),
            text(logger),
            text(message),
        ]));
    }
}

fn error() -> PortError {
    PortError("RuntimeError".to_owned())
}

impl World {
    fn event(&self, event: PyJson) {
        self.events.lock().unwrap().push(event);
    }

    fn write_fails(state: &mut State) -> bool {
        let index = state.write_calls;
        state.write_calls += 1;
        state.fail_writes || state.fail_write_calls.contains(&index)
    }

    fn apply(&self, settings: &PyJson) {
        let mut state = self.state.lock().unwrap();
        state.fail_write_calls.clear();
        state.deliver_calls.clear();
        let strings = |value: &PyJson| -> Vec<String> {
            value
                .as_list()
                .unwrap()
                .iter()
                .map(|item| item.as_str().unwrap().to_owned())
                .collect()
        };
        for (key, value) in settings.as_dict().unwrap() {
            match key.as_str() {
                "down_paths" => state.down_paths = strings(value),
                "down_targets" => state.down_targets = strings(value),
                "raise_targets" => state.raise_targets = strings(value),
                "fail_reads" => state.fail_reads = value.as_bool().unwrap(),
                "fail_writes" => state.fail_writes = value.as_bool().unwrap(),
                "deliver" => state.deliver = value.as_str().unwrap().to_owned(),
                "deliver_calls" => state.deliver_calls = strings(value),
                "fail_write_calls" => {
                    state.fail_write_calls = value
                        .as_list()
                        .unwrap()
                        .iter()
                        .map(|v| v.as_i64().unwrap())
                        .collect();
                }
                "held" => {
                    state.held = value
                        .as_list()
                        .unwrap()
                        .iter()
                        .map(|row| HeldCheck {
                            check_id: row.int_field("check_id"),
                            data: loads(row.str_field("data_json")).unwrap(),
                            status: row.str_field("status").to_owned(),
                            disabled: row.bool_field("disabled"),
                            next_check_time: row.int_field("next_check_time"),
                            held_since: row.int_field("held_since"),
                        })
                        .collect();
                }
                "in_flight" => state.in_flight = value.as_i64().unwrap(),
                "held_at_close" => state.held_at_close = value.as_i64(),
                "fail_in_flight" => state.fail_in_flight = value.as_bool().unwrap(),
                "fail_held" => state.fail_held = value.as_bool().unwrap(),
                "fail_reschedule" => state.fail_reschedule = value.as_bool().unwrap(),
                "fail_count_held" => state.fail_count_held = value.as_bool().unwrap(),
                other => panic!("unknown setting {other}"),
            }
        }
        state.write_calls = 0;
        state.probed.clear();
        self.events.lock().unwrap().clear();
    }
}

impl SitePorts for World {
    fn now(&self) -> i64 {
        self.state.lock().unwrap().now
    }

    fn probe(&self, targets: &[ProbeTarget]) -> impl Future<Output = Vec<ProbeOutcome>> + Send {
        let mut state = self.state.lock().unwrap();
        let outcomes = targets
            .iter()
            .map(|target| {
                let name = target.to_string();
                state.probed.push(name.clone());
                if state.raise_targets.contains(&name) {
                    ProbeOutcome::Raised(error())
                } else if state.down_targets.contains(&name)
                    || state.down_paths.iter().any(|path| path == target.path)
                {
                    ProbeOutcome::Failed
                } else {
                    ProbeOutcome::Answered
                }
            })
            .collect();
        async move { outcomes }
    }

    fn get_incident(&self) -> impl Future<Output = Result<Option<PyDict>, PortError>> + Send {
        let state = self.state.lock().unwrap();
        let result = if state.fail_reads {
            self.event(list([text("store"), text("get"), text("error")]));
            Err(error())
        } else {
            let found = if state.row.is_some() { "ok" } else { "none" };
            self.event(list([text("store"), text("get"), text(found)]));
            Ok(state.row.clone())
        };
        async move { result }
    }

    fn set_incident_payload(
        &self,
        payload: &PyDict,
    ) -> impl Future<Output = Result<bool, PortError>> + Send {
        let mut state = self.state.lock().unwrap();
        let body = payload_text(payload);
        let (outcome, result) = if World::write_fails(&mut state) {
            ("error", Err(error()))
        } else if state.row.is_none() {
            ("none", Ok(false))
        } else {
            state.row = Some(payload.clone());
            ("ok", Ok(true))
        };
        self.event(list([
            text("store"),
            text("set"),
            text(outcome),
            text(&body),
        ]));
        async move { result }
    }

    fn open_incident(
        &self,
        now: i64,
        payload: &PyDict,
    ) -> impl Future<Output = Result<(), PortError>> + Send {
        let mut state = self.state.lock().unwrap();
        let body = payload_text(payload);
        let (outcome, result) = if World::write_fails(&mut state) {
            ("error", Err(error()))
        } else {
            state.row = Some(payload.clone());
            ("ok", Ok(()))
        };
        self.event(list([
            text("store"),
            text("open"),
            text(outcome),
            text(&body),
            PyJson::Int(now),
        ]));
        async move { result }
    }

    fn count_held_checks(&self) -> impl Future<Output = Result<i64, PortError>> + Send {
        let state = self.state.lock().unwrap();
        self.event(list([text("sched"), text("count_held")]));
        let result = if state.fail_count_held {
            Err(error())
        } else {
            Ok(state.held_at_close.unwrap_or(state.held.len() as i64))
        };
        async move { result }
    }

    fn count_processing_claims_before(
        &self,
        epoch: i64,
    ) -> impl Future<Output = Result<i64, PortError>> + Send {
        let state = self.state.lock().unwrap();
        self.event(list([text("sched"), text("in_flight"), PyJson::Int(epoch)]));
        let result = if state.fail_in_flight {
            Err(error())
        } else {
            Ok(state.in_flight)
        };
        async move { result }
    }

    fn list_held_checks(&self) -> impl Future<Output = Result<Vec<HeldCheck>, PortError>> + Send {
        let state = self.state.lock().unwrap();
        self.event(list([text("sched"), text("held")]));
        let result = if state.fail_held {
            Err(error())
        } else {
            Ok(state.held.clone())
        };
        async move { result }
    }

    fn reschedule_checks(
        &self,
        check_ids: &[i64],
        run_at: i64,
    ) -> impl Future<Output = Result<i64, PortError>> + Send {
        let state = self.state.lock().unwrap();
        self.event(list([
            text("sched"),
            text("reschedule"),
            list(check_ids.iter().map(|id| PyJson::Int(*id))),
            PyJson::Int(run_at),
        ]));
        let result = if state.fail_reschedule {
            Err(error())
        } else {
            Ok(check_ids.len() as i64)
        };
        async move { result }
    }

    fn notify(
        &self,
        message: &SiteMessage,
    ) -> impl Future<Output = Result<SiteDelivery, PortError>> + Send {
        let mut state = self.state.lock().unwrap();
        self.event(list([
            text("send"),
            text(message.incident_key),
            text(message.name),
            text(message.error_type),
            text(&message.error_msg),
            text(message.status),
            PyJson::Bool(message.opsgate_ticket),
        ]));
        let outcome = if state.deliver_calls.is_empty() {
            state.deliver.clone()
        } else {
            state.deliver_calls.remove(0)
        };
        let result = match outcome.as_str() {
            "delivered" => Ok(SiteDelivery::Delivered),
            "failed" => Ok(SiteDelivery::Failed),
            "unknown" => Ok(SiteDelivery::Unknown),
            "raise" => Err(PortError("telegram exploded".to_owned())),
            other => panic!("unknown delivery {other}"),
        };
        async move { result }
    }

    fn publish(&self, _snapshot: &SiteSnapshot) {}

    fn log(&self, level: LogLevel, message: &str, _error: Option<&PortError>) {
        self.event(list([
            text("log"),
            text(level.as_str()),
            text(super::observer::LOGGER),
            text(message),
        ]));
    }
}

fn targets(spec: &PyJson, path: &'static str) -> Vec<ProbeTarget> {
    spec.list_field(path)
        .iter()
        .map(|item| match item {
            PyJson::Str(name) => ProbeTarget {
                path,
                kind: ProbeKind::Dns,
                host: name.clone(),
                port: 0,
            },
            endpoint => {
                let parts = endpoint.as_list().unwrap();
                ProbeTarget {
                    path,
                    kind: ProbeKind::Tcp,
                    host: parts[0].as_str().unwrap().to_owned(),
                    port: u16::try_from(parts[1].as_i64().unwrap()).unwrap(),
                }
            }
        })
        .collect()
}

fn config(spec: &PyJson) -> SiteEnvConfig {
    SiteEnvConfig {
        mode: SiteMode::parse(spec.str_field("mode")).unwrap(),
        probe_interval: spec.int_field("probe_interval"),
        probe_timeout: spec.int_field("probe_timeout"),
        ipv4_targets: targets(spec, "ipv4"),
        ipv6_targets: targets(spec, "ipv6"),
        dns_names: targets(spec, "dns"),
        down_after_failures: spec.int_field("down_after_failures"),
        recovery_grace_seconds: spec.int_field("recovery_grace_seconds"),
        max_hold_seconds: spec.int_field("max_hold_seconds"),
        incident_notify_after_seconds: spec.int_field("incident_notify_after_seconds"),
        incident_reminder_seconds: spec.int_field("incident_reminder_seconds"),
    }
}

fn snapshot_json(snapshot: &SiteSnapshot) -> PyJson {
    let mut paths = PyDict::new();
    for name in super::dependency::PathName::ALL {
        let path = snapshot.path(name).expect("every path is known");
        paths.insert(
            name.as_str().to_owned(),
            list([
                text(path.state.as_str()),
                PyJson::Int(path.down_since),
                PyJson::Int(path.recovered_at),
                PyJson::Int(path.release_at),
                PyJson::Int(path.last_release_at),
            ]),
        );
    }
    list([
        PyJson::Int(snapshot.observed_at),
        text(snapshot.mode.as_str()),
        PyJson::Int(snapshot.incident_id),
        PyJson::Dict(paths),
    ])
}

fn replay(group: &str, scenario: &PyJson) -> usize {
    let name = format!("{group}/{}", scenario.str_field("name"));
    let config = config(scenario.field("config"));
    let world = World::default();
    world.state.lock().unwrap().deliver = "delivered".to_owned();
    let warnings = Arc::new(WarnOnce::new(Arc::new(EventSink(world.events.clone()))));
    let mut observer = Observer::new(config.clone(), warnings.clone());
    let results = scenario.list_field("results");
    let mut index = 0;
    for step in scenario.list_field("steps") {
        match step.str_field("op") {
            "seed" => {
                let payload = loads(step.str_field("payload_json")).unwrap();
                world.state.lock().unwrap().row = Some(payload.into_dict().unwrap_or_default());
                continue;
            }
            "drop_row" => {
                world.state.lock().unwrap().row = None;
                continue;
            }
            _ => {}
        }
        world.apply(step.get("set").unwrap_or(&PyJson::dict()));
        world.state.lock().unwrap().now = step.int_field("at");
        let outcome: Result<(), Raised> = match step.str_field("op") {
            "respawn" => {
                observer = Observer::new(config.clone(), warnings.clone());
                block_on(observer.restore(&world))
            }
            "restore" => block_on(observer.restore(&world)),
            "round" => block_on(observer.run_once(&world)),
            other => panic!("unknown op {other}"),
        };
        let expected = &results[index];
        let context = format!("{name} step {index} at {}", step.int_field("at"));
        let events = PyJson::List(world.events.lock().unwrap().clone());
        let expected_events = expected.field("events");
        if &events != expected_events {
            let got = events.as_list().unwrap();
            let want = expected_events.as_list().unwrap();
            for (position, (g, w)) in got.iter().zip(want).enumerate() {
                assert_eq!(g, w, "{context}: event {position}");
            }
            panic!(
                "{context}: {} events, expected {}: {got:?} vs {want:?}",
                got.len(),
                want.len()
            );
        }
        let probed = list(world.state.lock().unwrap().probed.iter().map(|t| text(t)));
        assert_eq!(&probed, expected.field("probed"), "{context}: probed");
        assert_eq!(
            &snapshot_json(observer.snapshot()),
            expected.field("snapshot"),
            "{context}: snapshot"
        );
        assert_eq!(
            outcome.err().map(|raised| raised.0),
            expected.opt_str("raised"),
            "{context}: raised"
        );
        index += 1;
    }
    assert_eq!(index, results.len(), "{name}: result count");
    index
}

#[test]
fn site_replay_matches_python() {
    let mut steps = 0;
    let mut scenarios = 0;
    for (group, text) in FIXTURES {
        let fixture = load(text);
        for scenario in fixture.list_field("scenarios") {
            steps += replay(group, scenario);
            scenarios += 1;
        }
    }
    assert!(scenarios >= 40, "{scenarios} scenarios");
    assert!(steps >= 900, "{steps} steps");
}

#[test]
fn observer_futures_are_send() {
    fn assert_send<T: Send>(_: T) {}
    let config = SiteEnvConfig::default();
    let world = World::default();
    let warnings = Arc::new(WarnOnce::new(Arc::new(EventSink(world.events.clone()))));
    let mut observer = Observer::new(config, warnings);
    assert_send(observer.run_once(&world));
}

#[test]
fn off_mode_observer_has_unobserved_paths_only_without_targets() {
    let mut config = SiteEnvConfig::default();
    config.ipv6_targets.clear();
    let world = World::default();
    let warnings = Arc::new(WarnOnce::new(Arc::new(EventSink(world.events.clone()))));
    let observer = Observer::new(config, warnings);
    assert_eq!(
        observer.unobserved_paths(),
        vec![super::dependency::PathName::Ipv6]
    );
    let payload = observer.build_payload();
    let paths = payload.get("paths").and_then(PyJson::as_dict).unwrap();
    assert_eq!(paths.keys().collect::<Vec<_>>(), ["dns", "ipv4"]);
}

/// `test_restart_after_every_single_write_is_consistent`: every payload the
/// replays wrote restores to the same lifecycle.
#[test]
fn every_written_payload_restores_to_itself() {
    let mut checked = 0;
    for (_, text) in FIXTURES {
        let fixture = load(text);
        for scenario in fixture.list_field("scenarios") {
            let config = config(scenario.field("config"));
            for result in scenario.list_field("results") {
                for event in result.list_field("events") {
                    let event = event.as_list().unwrap();
                    if event[0].as_str() != Some("store") || event[2].as_str() != Some("ok") {
                        continue;
                    }
                    let Some(body) = event.get(3).and_then(PyJson::as_str) else {
                        continue;
                    };
                    let payload = loads(body).unwrap().into_dict().unwrap();
                    let world = World::default();
                    {
                        let mut state = world.state.lock().unwrap();
                        state.now = payload.get("observed_at").unwrap().as_i64().unwrap() + 10;
                        state.row = Some(payload.clone());
                    }
                    let warnings =
                        Arc::new(WarnOnce::new(Arc::new(EventSink(world.events.clone()))));
                    let mut observer = Observer::new(config.clone(), warnings);
                    block_on(observer.restore(&world)).unwrap();
                    assert_eq!(observer.build_payload(), payload, "{body}");
                    checked += 1;
                }
            }
        }
    }
    assert!(checked > 500, "{checked} payloads");
}
