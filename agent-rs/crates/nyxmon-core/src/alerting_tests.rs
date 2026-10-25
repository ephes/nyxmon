//! Fixture tests of the alerting core (slice S3): policy, site dependency,
//! site snapshot, suppression rules, the decision function, the runtime
//! estimate and the delivery-marker helpers, each against the file
//! the former `fixtures/generate.py` (`gen_alerting.py`, removed 2026-10-25)
//! produced from the Python code.

use std::collections::HashMap;
use std::sync::Arc;

use num_bigint::BigInt;

use crate::decision::{DecisionContext, SiteLimits, decide};
use crate::env::{Env, EnvSource, SiteMode};
use crate::fixture::{Field, load};
use crate::incident::{carry_delivery_markers, with_delivery_intent, without_delivery_markers};
use crate::lease::estimated_check_runtime_seconds;
use crate::log::{RecordingSink, WarnOnce};
use crate::model::{CHECK_STATUS_IDLE, Check, NotificationState, ResultStatus};
use crate::policy::resolve_notification_policy;
use crate::pyjson::{PyDict, PyJson, loads, py_float_repr, py_repr, py_str};
use crate::site::{
    PathName, PathSnapshot, PathState, SiteSnapshot, resolve_site_dependency,
    warn_unobserved_requirements,
};
use crate::suppression::{
    finite_seconds, int_or_none, lookup, notification_suppression_details, rule_matches,
    suppression_request, timeout_seconds,
};

fn json(case: &PyJson, key: &str) -> PyJson {
    loads(case.str_field(key)).expect("fixture JSON text")
}

fn dict(case: &PyJson, key: &str) -> PyDict {
    json(case, key).into_dict().expect("fixture JSON object")
}

fn dumps(value: &PyJson) -> String {
    value.to_json_string().expect("serialisable")
}

fn dumps_dict(value: &PyDict) -> String {
    dumps(&PyJson::Dict(value.clone()))
}

fn env_map(value: Option<&PyJson>) -> HashMap<String, String> {
    value
        .and_then(PyJson::as_dict)
        .map(|map| {
            map.iter()
                .map(|(k, v)| (k.clone(), v.as_str().expect("env value").to_owned()))
                .collect()
        })
        .unwrap_or_default()
}

fn warnings(case: &PyJson) -> Vec<(String, String)> {
    case.list_field("warnings")
        .iter()
        .map(|pair| {
            let pair = pair.as_list().expect("[logger, message]");
            (
                pair[0].as_str().expect("logger").to_owned(),
                pair[1].as_str().expect("message").to_owned(),
            )
        })
        .collect()
}

/// A recording sink, a [`WarnOnce`] and an [`Env`] sharing it.
fn harness() -> (Arc<RecordingSink>, WarnOnce, Env) {
    let sink = Arc::new(RecordingSink::default());
    let warnings = WarnOnce::new(sink.clone());
    let env = Env::from_map(HashMap::<String, String>::new(), sink.clone());
    (sink, warnings, env)
}

fn status(name: &str) -> ResultStatus {
    ResultStatus::parse(name).expect("result status")
}

#[test]
fn policy_matches_python() {
    let fixture = load(include_str!("../../../fixtures/policy.json"));
    let mut count = 0;
    for scenario in fixture.list_field("scenarios") {
        let (sink, warn_once, mut env) = harness();
        for call in scenario.as_list().expect("calls") {
            env.replace_source(EnvSource::Map(env_map(call.get("env"))));
            let data = json(call, "data_json");
            let policy = resolve_notification_policy(
                &env,
                &warn_once,
                call.int_field("check_id"),
                &data,
                status(call.str_field("status")),
            );
            let expected = call.field("value");
            assert_eq!(
                (policy.consecutive_failures, policy.reminder_seconds),
                (
                    expected.int_field("consecutive_failures"),
                    expected.int_field("reminder_seconds")
                ),
                "{call:?}"
            );
            assert_eq!(sink.take(), warnings(call), "{call:?}");
            count += 1;
        }
    }
    assert!(count > 200);
}

fn requirements_json(requirements: &[Vec<PathName>]) -> PyJson {
    PyJson::List(
        requirements
            .iter()
            .map(|r| PyJson::List(r.iter().map(|p| PyJson::from(p.as_str())).collect()))
            .collect(),
    )
}

#[test]
fn site_dependency_matches_python() {
    let fixture = load(include_str!("../../../fixtures/site_dependency.json"));
    for scenario in fixture.list_field("scenarios") {
        let (sink, warn_once, _env) = harness();
        for call in scenario.as_list().expect("calls") {
            let data = json(call, "data_json");
            let dependency = resolve_site_dependency(&warn_once, call.int_field("check_id"), &data);
            match (dependency, call.field("value")) {
                (None, PyJson::Null) => {}
                (Some(dependency), expected) => {
                    assert_eq!(
                        requirements_json(&dependency.requirements),
                        *expected.field("requirements"),
                        "{call:?}"
                    );
                    let names: Vec<PyJson> = dependency
                        .path_names()
                        .iter()
                        .map(|p| PyJson::from(p.as_str()))
                        .collect();
                    assert_eq!(PyJson::List(names), *expected.field("path_names"));
                }
                (None, expected) => panic!("expected {expected:?} for {call:?}"),
            }
            assert_eq!(sink.take(), warnings(call), "{call:?}");
        }
    }

    let (sink, warn_once, _env) = harness();
    for call in fixture.list_field("unobserved") {
        let checks: Vec<(i64, PyJson)> = call
            .list_field("checks")
            .iter()
            .map(|pair| {
                let pair = pair.as_list().expect("[id, data]");
                let data = loads(pair[1].as_str().expect("data")).expect("json");
                (pair[0].as_i64().expect("id"), data)
            })
            .collect();
        let unobserved: Vec<PathName> = call
            .list_field("unobserved")
            .iter()
            .map(|name| PathName::parse(name.as_str().expect("name")).expect("path"))
            .collect();
        warn_unobserved_requirements(
            &warn_once,
            checks.iter().map(|(id, data)| (*id, data)),
            &unobserved,
        );
        assert_eq!(sink.take(), warnings(call), "{call:?}");
    }
}

fn snapshot(spec: &PyJson) -> SiteSnapshot {
    let mut paths = [None; 3];
    for (name, values) in spec.field("paths").as_dict().expect("paths") {
        let values = values.as_list().expect("path values");
        let int = |index: usize| values[index].as_i64().expect("timestamp");
        let state = PathState::parse(values[0].as_str().expect("state")).expect("path state");
        paths[PathName::parse(name).expect("path name").index()] = Some(PathSnapshot {
            state,
            down_since: int(1),
            recovered_at: int(2),
            release_at: int(3),
            last_release_at: int(4),
        });
    }
    SiteSnapshot {
        paths,
        observed_at: spec.int_field("observed_at"),
        mode: SiteMode::parse(spec.str_field("mode")).expect("mode"),
        incident_id: spec.int_field("incident_id"),
    }
}

fn reason_json(reason: Option<crate::site::HoldReason>) -> Option<String> {
    reason.map(|reason| dumps_dict(&reason.to_dict()))
}

#[test]
fn site_snapshot_matches_python() {
    let fixture = load(include_str!("../../../fixtures/site_snapshot.json"));
    let (_sink, warn_once, _env) = harness();
    let cases = fixture.list_field("cases");
    assert!(cases.len() > 500);
    for case in cases {
        let snap = snapshot(case.field("snapshot"));
        let raw = json(case, "dependency_json");
        let mut data = PyDict::new();
        if !raw.is_null() {
            data.insert("site_dependency".to_owned(), raw);
        }
        let dependency = resolve_site_dependency(&warn_once, 0, &PyJson::Dict(data));
        let dep = dependency.as_ref();
        let claim = case.int_field("claim_started_at");
        let now = case.int_field("now");
        let stale = case.int_field("stale_after");
        assert_eq!(
            reason_json(snap.hold_reason(dep, claim, now, stale)).as_deref(),
            case.opt_str("hold_reason_json"),
            "hold {case:?}"
        );
        assert_eq!(
            reason_json(snap.observed_reason(dep, claim, now, stale)).as_deref(),
            case.opt_str("observed_reason_json"),
            "observed {case:?}"
        );
        assert_eq!(
            snap.dependency_recovered(dep, claim, now, stale),
            case.bool_field("dependency_recovered")
        );
        assert_eq!(
            snap.dependency_usable(dep),
            case.bool_field("dependency_usable")
        );
    }
}

fn outcome<T>(
    case: &PyJson,
    actual: Result<Option<T>, crate::pyjson::PyNumError>,
    render: impl Fn(T) -> String,
) {
    match actual {
        Err(error) => assert_eq!(
            Some(error.python_class()),
            case.opt_str("error"),
            "{case:?}"
        ),
        Ok(None) => assert!(case.bool_field("missing"), "{case:?}"),
        Ok(Some(value)) => {
            assert!(!case.bool_field("missing"), "{case:?}");
            assert_eq!(render(value), case.str_field("value_json"), "{case:?}");
        }
    }
}

#[test]
fn suppression_rules_match_python() {
    let fixture = load(include_str!("../../../fixtures/suppression_rules.json"));
    for case in fixture.list_field("lookups") {
        let payload = json(case, "payload_json");
        outcome(case, lookup(&payload, case.str_field("path")), dumps);
    }

    let rule_payload = loads(
        r#"{"status": "running", "count": 3, "ratio": 0.5, "flag": true, "nothing": null,
            "items": [1, [2, 3]], "obj": {"x": 1}, "nan": NaN, "big": 10000000000000000000000000}"#,
    )
    .expect("payload");
    for case in fixture.list_field("rule_matches") {
        let rule = dict(case, "rule_json");
        let actual = rule_matches(&rule_payload, &rule).map(|matched| Some(PyJson::Bool(matched)));
        outcome(case, actual, |v| dumps(&v));
    }

    for case in fixture.list_field("scalars") {
        let value = json(case, "value_json");
        assert_eq!(
            finite_seconds(&value).map(py_float_repr).as_deref(),
            case.opt_str("finite_seconds"),
            "{case:?}"
        );
        assert_eq!(
            int_or_none(&value).map(|n| n.to_string()).as_deref(),
            case.opt_str("int_or_none"),
            "{case:?}"
        );
        let expected = case.field("timeout_seconds");
        match timeout_seconds(Some(&value)) {
            Ok(seconds) => assert_eq!(py_float_repr(seconds), expected.str_field("value")),
            Err(error) => assert_eq!(error.python_class(), expected.str_field("error")),
        }
    }
    assert_eq!(timeout_seconds(None).ok(), Some(3.0));

    for case in fixture.list_field("value_text") {
        let value = json(case, "value_json");
        assert_eq!(py_repr(&value), case.str_field("repr"));
        assert_eq!(py_str(&value), case.str_field("str"));
    }

    for case in fixture.list_field("details") {
        let data = json(case, "data_json");
        let payload = json(case, "payload_json");
        let mut requests = Vec::new();
        let actual = notification_suppression_details(&data, case.int_field("now"), |request| {
            requests.push(request.clone());
            case.bool_field("fetch_ok").then(|| payload.clone())
        });
        let expected_requests = case.list_field("requests");
        assert_eq!(requests.len(), expected_requests.len(), "{case:?}");
        for (request, expected) in requests.iter().zip(expected_requests) {
            assert_eq!(request.url, expected.str_field("url"));
            assert_eq!(
                py_float_repr(request.timeout),
                expected.str_field("timeout")
            );
            let auth = match &request.auth {
                Some((user, password)) => PyJson::List(vec![
                    PyJson::from(user.as_str()),
                    PyJson::from(password.as_str()),
                ]),
                None => PyJson::Null,
            };
            assert_eq!(auth, *expected.field("auth"), "{case:?}");
        }
        match actual {
            Ok(details) => assert_eq!(
                details.as_ref().map(dumps_dict).as_deref(),
                case.opt_str("details_json"),
                "{case:?}"
            ),
            Err(error) => assert_eq!(Some(error.python_class()), case.opt_str("error")),
        }
    }
}

#[test]
fn suppression_ignores_a_non_object_data_column() {
    // PLAN.md D-11: Python would raise on `check.data.get`; Rust treats a
    // non-object as "no configuration".
    for text in ["null", "[]", "\"x\"", "5"] {
        let data = loads(text).expect("json");
        assert_eq!(suppression_request(&data), Ok(None));
    }
}

fn state(value: &PyJson) -> NotificationState {
    let row: Vec<Option<i64>> = value
        .as_list()
        .expect("state")
        .iter()
        .map(PyJson::as_i64)
        .collect();
    NotificationState::from_row(Some(&row))
}

#[test]
fn decision_matches_python() {
    let fixture = load(include_str!("../../../fixtures/decision.json"));
    let cases = fixture.list_field("cases");
    assert!(cases.len() >= 1000);
    for case in cases {
        let read = state(case.field("state"));
        let Some(result_status) = ResultStatus::parse(case.str_field("status")) else {
            // Python's "neither error nor warning" branch: unreachable with
            // the Rust status type; the state stays unchanged.
            assert!(!case.bool_field("should_notify"));
            assert_eq!(state(case.field("next_state")), read);
            continue;
        };
        let (sink, warn_once, mut env) = harness();
        env.replace_source(EnvSource::Map(env_map(case.get("env"))));
        let claim = case.int_field("claim_started_at");
        let check = Check {
            check_id: case.int_field("check_id"),
            service_id: 1,
            name: String::new(),
            check_type: "http".to_owned(),
            url: "https://example.invalid".to_owned(),
            check_interval: 300,
            next_check_time: 0,
            processing_started_at: claim,
            status: CHECK_STATUS_IDLE.to_owned(),
            disabled: false,
            data: json(case, "check_data_json"),
            claim_started_at: claim,
        };
        let site = case.field("site");
        let snap = site.get("snapshot").filter(|s| !s.is_null()).map(snapshot);
        let limits = match site.get("config") {
            Some(config) if !config.is_null() => SiteLimits {
                stale_after: 3 * config.int_field("probe_interval"),
                max_hold: config.int_field("max_hold_seconds"),
            },
            _ => SiteLimits::default(),
        };
        let cooldown = match case.field("immediate_cooldown") {
            PyJson::Int(value) => BigInt::from(*value),
            PyJson::BigInt(value) => value.clone(),
            other => panic!("cooldown {other:?}"),
        };
        let ctx = DecisionContext {
            env: &env,
            warnings: &warn_once,
            snapshot: snap.as_ref(),
            limits,
            retry_seconds: case.int_field("retry_seconds"),
            immediate_cooldown: &cooldown,
        };
        let mut data = dict(case, "result_data_json");
        let decision = decide(
            &ctx,
            read,
            &check,
            result_status,
            &mut data,
            case.bool_field("force_notification"),
            case.int_field("now"),
        );
        assert_eq!(
            decision.should_notify,
            case.bool_field("should_notify"),
            "{case:?}"
        );
        assert_eq!(decision.expected, state(case.field("expected")));
        assert_eq!(
            decision.next_state,
            state(case.field("next_state")),
            "{case:?}"
        );
        assert_eq!(decision.held, case.bool_field("held"));
        assert_eq!(decision.attempt_seq, case.int_field("attempt_seq"));
        assert_eq!(decision.now, case.int_field("decision_now"));
        assert_eq!(
            dumps_dict(&data),
            case.str_field("annotated_data_json"),
            "{case:?}"
        );
        assert_eq!(sink.take(), warnings(case), "{case:?}");
    }
}

#[test]
fn site_limits_default_to_the_documented_config() {
    assert_eq!(
        SiteLimits::default(),
        SiteLimits {
            stale_after: 180,
            max_hold: 10800
        }
    );
}

#[test]
fn lease_estimate_matches_python() {
    let fixture = load(include_str!("../../../fixtures/lease.json"));
    for case in fixture.list_field("cases") {
        let (sink, warn_once, _env) = harness();
        let data = json(case, "data_json");
        let keys = case.bool_field("with_warning_keys").then_some(&warn_once);
        let check_type = case.str_field("check_type");
        let seconds: Vec<PyJson> = (0..2)
            .map(|_| PyJson::Int(estimated_check_runtime_seconds(9, check_type, &data, keys)))
            .collect();
        assert_eq!(PyJson::List(seconds), *case.field("seconds"), "{case:?}");
        assert_eq!(sink.take(), warnings(case), "{case:?}");
    }
}

#[test]
fn delivery_markers_match_python() {
    let fixture = load(include_str!("../../../fixtures/incident_payloads.json"));
    let markers = fixture.field("delivery_markers");
    for case in markers.list_field("carry") {
        let carried =
            carry_delivery_markers(&dict(case, "payload_json"), &dict(case, "existing_json"));
        assert_eq!(dumps_dict(&carried), case.str_field("carried_json"));
    }
    for case in markers.list_field("intent") {
        let payload = dict(case, "payload_json");
        let intent = with_delivery_intent(&payload, case.int_field("attempt"));
        assert_eq!(dumps_dict(&intent), case.str_field("with_intent_json"));
        assert_eq!(
            dumps_dict(&without_delivery_markers(&payload)),
            case.str_field("without_markers_json")
        );
    }
}

#[test]
fn list_indexes_accept_ascii_digits_only() {
    // PLAN.md D-6: Python's `str.isdigit()` also accepts e.g. U+0661.
    let payload = loads("[[1, 2]]").expect("json");
    assert_eq!(lookup(&payload, "0.1"), Ok(Some(&PyJson::Int(2))));
    assert_eq!(lookup(&payload, "0.\u{661}"), Ok(None));
}
