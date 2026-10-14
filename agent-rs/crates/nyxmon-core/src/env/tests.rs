//! Environment readers against `agent-rs/fixtures/env.json` (generated from
//! the Python readers, warnings included).

use std::collections::HashMap;
use std::sync::Arc;

use super::*;
use crate::fixture::{Field, load};
use crate::log::RecordingSink;
use crate::pyjson::{PyJson, py_float_repr};

fn targets_json(targets: &[ProbeTarget]) -> PyJson {
    PyJson::List(
        targets
            .iter()
            .map(|t| {
                PyJson::List(vec![
                    PyJson::from(t.path),
                    PyJson::from(t.kind.as_str()),
                    PyJson::from(t.host.as_str()),
                    PyJson::Int(i64::from(t.port)),
                    PyJson::from(t.to_string()),
                ])
            })
            .collect(),
    )
}

fn dict(entries: Vec<(&str, PyJson)>) -> PyJson {
    PyJson::Dict(
        entries
            .into_iter()
            .map(|(k, v)| (k.to_owned(), v))
            .collect(),
    )
}

fn opt_str(value: &Option<String>) -> PyJson {
    value.as_deref().map_or(PyJson::Null, PyJson::from)
}

/// Run one reader and render its result like the generator does.
fn read(reader: &str, env: &Env) -> PyJson {
    match reader {
        "check_batch_size" => PyJson::Int(check_batch_size(env)),
        "processing_lease_seconds" => PyJson::from_bigint(processing_lease_seconds(env)),
        "shutdown_grace_seconds" => PyJson::from(py_float_repr(shutdown_grace_seconds(env))),
        "global_consecutive_failures" => PyJson::Int(global_consecutive_failures(env)),
        "global_reminder_seconds:error" => {
            PyJson::Int(global_reminder_seconds(env, ResultStatus::Error))
        }
        "global_reminder_seconds:warning" => {
            PyJson::Int(global_reminder_seconds(env, ResultStatus::Warning))
        }
        "notify_immediate_cooldown_seconds" => {
            PyJson::from_bigint(notify_immediate_cooldown_seconds(env))
        }
        "delivery_retry_seconds" => PyJson::Int(delivery_retry_seconds(env)),
        "site_config" => {
            let c = site_env_config(env);
            dict(vec![
                ("dns_names", targets_json(&c.dns_names)),
                ("down_after_failures", PyJson::Int(c.down_after_failures)),
                (
                    "incident_notify_after_seconds",
                    PyJson::Int(c.incident_notify_after_seconds),
                ),
                (
                    "incident_reminder_seconds",
                    PyJson::Int(c.incident_reminder_seconds),
                ),
                ("ipv4_targets", targets_json(&c.ipv4_targets)),
                ("ipv6_targets", targets_json(&c.ipv6_targets)),
                ("max_hold_seconds", PyJson::Int(c.max_hold_seconds)),
                ("mode", PyJson::from(c.mode.as_str())),
                ("probe_interval", PyJson::Int(c.probe_interval)),
                ("probe_timeout", PyJson::Int(c.probe_timeout)),
                (
                    "recovery_grace_seconds",
                    PyJson::Int(c.recovery_grace_seconds),
                ),
                ("stale_after_seconds", PyJson::Int(c.stale_after_seconds())),
            ])
        }
        "notifier_config" => {
            let c = match notifier_env(env) {
                Ok(c) => c,
                Err(err) => return dict(vec![("error", PyJson::from(err.python_class()))]),
            };
            dict(vec![
                (
                    "opsgate_approval_base_url",
                    PyJson::from(c.opsgate_approval_base_url.as_str()),
                ),
                ("opsgate_enabled", PyJson::Bool(c.opsgate_enabled())),
                (
                    "opsgate_include_warnings",
                    PyJson::Bool(c.opsgate_include_warnings),
                ),
                (
                    "opsgate_submit_base_url",
                    PyJson::from(c.opsgate_submit_base_url.as_str()),
                ),
                (
                    "opsgate_submit_timeout_seconds",
                    PyJson::from(py_float_repr(c.opsgate_submit_timeout_seconds)),
                ),
                (
                    "opsgate_submit_token",
                    PyJson::from(c.opsgate_submit_token.as_str()),
                ),
                (
                    "opsgate_ticket_expires_seconds",
                    PyJson::from_bigint(c.opsgate_ticket_expires_seconds),
                ),
                ("telegram_chat_id", opt_str(&c.telegram_chat_id)),
                ("telegram_token", opt_str(&c.telegram_token)),
            ])
        }
        other => panic!("unknown reader {other}"),
    }
}

fn env_map(value: &PyJson) -> HashMap<String, String> {
    value
        .as_dict()
        .expect("env dict")
        .iter()
        .map(|(k, v)| (k.clone(), v.as_str().expect("env value").to_owned()))
        .collect()
}

#[test]
fn env_readers_match_python() {
    let fixture = load(include_str!("../../../../fixtures/env.json"));
    let scenarios = fixture.list_field("scenarios");
    assert!(scenarios.len() > 200);
    for scenario in scenarios {
        let reader = scenario.str_field("reader");
        let sink = Arc::new(RecordingSink::default());
        // One Env per scenario: the warn-once state spans its calls.
        let mut env = Env::new(EnvSource::Map(HashMap::new()), sink.clone());
        for call in scenario.list_field("calls") {
            env.replace_source(EnvSource::Map(env_map(call.field("env"))));
            let label = format!("{reader} with {:?}", call.field("env"));
            let value = read(reader, &env);
            let expected = call.field("value");
            let expected = match expected.as_dict() {
                // Compare dicts independent of key order (fixture keys are sorted).
                Some(map) => {
                    let mut sorted: Vec<_> = map.iter().collect();
                    sorted.sort_by(|a, b| a.0.cmp(b.0));
                    dict(
                        sorted
                            .into_iter()
                            .map(|(k, v)| (k.as_str(), v.clone()))
                            .collect(),
                    )
                }
                None => expected.clone(),
            };
            assert_eq!(value, expected, "{label}");
            let warnings: Vec<(String, String)> = call
                .list_field("warnings")
                .iter()
                .map(|w| {
                    let pair = w.as_list().expect("warning pair");
                    (
                        pair[0].as_str().unwrap().to_owned(),
                        pair[1].as_str().unwrap().to_owned(),
                    )
                })
                .collect();
            assert_eq!(sink.take(), warnings, "{label}");
        }
    }
}

#[test]
fn process_source_reads_real_variables() {
    let env = Env::process();
    assert_eq!(env.raw("NYXMON_SURELY_UNSET_VARIABLE_FOR_TESTS"), None);
}

#[test]
fn default_site_config_matches_documented_defaults() {
    let defaults = SiteEnvConfig::default();
    assert_eq!(defaults.mode, SiteMode::Off);
    assert_eq!(defaults.ipv4_targets.len(), 3);
    assert_eq!(
        defaults.ipv6_targets[0].to_string(),
        "[2606:4700:4700::1111]:443"
    );
    assert_eq!(defaults.dns_names[2].to_string(), "quad9.net");
    assert_eq!(defaults.stale_after_seconds(), 180);
}
