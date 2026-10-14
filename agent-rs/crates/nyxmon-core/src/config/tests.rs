//! Config parsers against `fixtures/config_http.json` and
//! `fixtures/config_json_metrics.json` (`cases`: `from_dict` + `validate`).

use super::http::HttpConfig;
use super::json_metrics::JsonMetricsConfig;
use super::*;
use crate::fixture::{Field, load};
use crate::pyjson::PyDict;

fn dict(entries: Vec<(&str, PyJson)>) -> PyJson {
    PyJson::Dict(
        entries
            .into_iter()
            .map(|(k, v)| (k.to_owned(), v))
            .collect::<PyDict>(),
    )
}

fn http_outcome(data: &PyJson) -> PyJson {
    match HttpConfig::from_data(data) {
        Ok(c) => dict(vec![(
            "ok",
            dict(vec![
                ("timeout", PyJson::Float(c.timeout)),
                ("retries", PyJson::Int(i64::from(c.retries))),
                ("retry_delay", PyJson::Float(c.retry_delay)),
                (
                    "retry_status_codes",
                    PyJson::List(
                        c.retry_status_codes
                            .iter()
                            .map(|s| PyJson::Int(*s))
                            .collect(),
                    ),
                ),
                ("follow_redirects", PyJson::Bool(c.follow_redirects)),
                (
                    "expected_status",
                    c.expected_status.map_or(PyJson::Null, PyJson::Int),
                ),
                (
                    "expected_location",
                    c.expected_location.map_or(PyJson::Null, PyJson::from),
                ),
            ]),
        )]),
        Err(message) => dict(vec![("error", PyJson::from(message))]),
    }
}

fn metrics_outcome(data: &PyJson) -> PyJson {
    match JsonMetricsConfig::from_data(data) {
        Ok(c) => dict(vec![(
            "ok",
            dict(vec![
                ("url", c.url.clone()),
                (
                    "checks",
                    PyJson::List(
                        c.checks
                            .iter()
                            .map(|check| {
                                dict(vec![
                                    ("path", check.path.clone()),
                                    ("op", PyJson::from(check.op.symbol())),
                                    ("value", check.value.clone()),
                                    ("severity", PyJson::from(check.severity.as_str())),
                                ])
                            })
                            .collect(),
                    ),
                ),
                ("timeout", PyJson::Float(c.timeout)),
                ("auth", c.auth.clone().unwrap_or(PyJson::Null)),
                ("retries", PyJson::from_bigint(c.retries.clone())),
                ("retry_delay", PyJson::Float(c.retry_delay)),
                (
                    "max_body_bytes",
                    PyJson::from_bigint(c.max_body_bytes.clone()),
                ),
            ]),
        )]),
        Err(ConfigError::Invalid(message)) => dict(vec![("error", PyJson::from(message))]),
        Err(ConfigError::Exception(exc)) => dict(vec![("exception", PyJson::from(exc.0))]),
    }
}

/// Fixture dicts are written with sorted keys; compare key-order-insensitively.
fn sorted(value: &PyJson) -> PyJson {
    match value {
        PyJson::Dict(map) => {
            let mut entries: Vec<(&String, &PyJson)> = map.iter().collect();
            entries.sort_by(|a, b| a.0.cmp(b.0));
            PyJson::Dict(
                entries
                    .into_iter()
                    .map(|(k, v)| (k.clone(), sorted(v)))
                    .collect(),
            )
        }
        PyJson::List(items) => PyJson::List(items.iter().map(sorted).collect()),
        other => other.clone(),
    }
}

fn check_cases(file: &str, outcome: fn(&PyJson) -> PyJson) {
    let fixture = load(file);
    let cases = fixture.list_field("cases");
    assert!(cases.len() > 100, "fixture has {} cases", cases.len());
    for case in cases {
        let data = case.field("data");
        assert_eq!(
            sorted(&outcome(data)),
            sorted(case.field("outcome")),
            "data: {data:?}"
        );
    }
}

#[test]
fn http_config_matches_python() {
    check_cases(
        include_str!("../../../../fixtures/config_http.json"),
        http_outcome,
    );
}

#[test]
fn json_metrics_config_matches_python() {
    check_cases(
        include_str!("../../../../fixtures/config_json_metrics.json"),
        metrics_outcome,
    );
}

#[test]
fn sorted_set_repr_matches_the_python_literal() {
    assert_eq!(
        sorted_set_repr(&["warning", "critical"]),
        "{'critical', 'warning'}"
    );
}
