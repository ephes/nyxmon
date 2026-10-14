//! json-metrics evaluation against `fixtures/metrics_eval.json`.

use super::*;
use crate::config::json_metrics::JsonMetricsConfig;
use crate::fixture::{Field, load};

fn fixture() -> PyJson {
    load(include_str!("../../../../fixtures/metrics_eval.json"))
}

/// Fixture dicts are written with sorted keys; compare key-order-insensitively
/// where the fixture cannot carry the order.
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

#[test]
fn resolve_path_matches_python() {
    let fixture = fixture();
    let cases = fixture.list_field("resolve");
    // The shared payload of the generator, rebuilt from the "$" case.
    let payload = cases
        .iter()
        .find(|c| c.str_field("path") == "$" && c.get("payload").is_none())
        .expect("$ case")
        .field("value")
        .clone();
    for case in cases {
        let doc = case.get("payload").unwrap_or(&payload);
        let path = case.str_field("path");
        match resolve_path(doc, path) {
            Err(exc) => assert_eq!(
                Some(exc.0),
                case.get("exception").and_then(PyJson::as_str),
                "{path}"
            ),
            Ok(value) => {
                assert!(
                    case.get("exception").is_none(),
                    "{path}: expected an exception"
                );
                assert_eq!(value.is_none(), case.bool_field("missing"), "{path}");
                if let Some(value) = value {
                    assert_eq!(sorted(value), sorted(case.field("value")), "{path}");
                }
            }
        }
    }
}

#[test]
fn bounded_actual_matches_python() {
    let fixture = fixture();
    for case in fixture.list_field("bounded_actual") {
        let (actual, truncated) = bounded_actual(case.field("actual"));
        let expected = case.field("result");
        assert_eq!(
            sorted(&actual),
            sorted(expected.field("actual")),
            "{case:?}"
        );
        assert_eq!(
            truncated,
            expected.get("actual_truncated").is_some(),
            "{case:?}"
        );
    }
}

#[test]
fn evaluate_matches_python() {
    let fixture = fixture();
    let cases = fixture.list_field("evaluate");
    assert!(cases.len() > 300);
    for case in cases {
        let mut data = crate::pyjson::PyDict::new();
        data.insert("url".to_owned(), PyJson::from("u"));
        data.insert("checks".to_owned(), case.field("checks").clone());
        let config = JsonMetricsConfig::from_data(&PyJson::Dict(data)).expect("valid checks");
        let payload = case.field("payload");
        match evaluate(payload, &config.checks) {
            Err(exc) => assert_eq!(
                Some(exc.0),
                case.get("exception").and_then(PyJson::as_str),
                "{case:?}"
            ),
            Ok(failures) => {
                assert!(case.get("exception").is_none(), "{case:?}");
                let expected = case.list_field("failures");
                assert_eq!(failures.len(), expected.len(), "{case:?}");
                for (got, want) in failures.iter().zip(expected) {
                    // Key order of a failure record (PLAN.md 1.6).
                    let keys: Vec<&str> =
                        got.as_dict().unwrap().keys().map(String::as_str).collect();
                    let order: &[&str] = if got.get("reason").is_some() {
                        &["path", "op", "expected", "actual", "reason", "severity"]
                    } else if got.get("actual_truncated").is_some() {
                        &[
                            "path",
                            "op",
                            "expected",
                            "actual",
                            "severity",
                            "actual_truncated",
                        ]
                    } else {
                        &["path", "op", "expected", "actual", "severity"]
                    };
                    assert_eq!(keys, order);
                    assert_eq!(sorted(got), sorted(want), "{case:?}");
                }
                assert_eq!(failure_summary(&failures), case.str_field("summary"));
                let status = case.opt_str("status");
                if failures.is_empty() {
                    assert_eq!(status, None);
                } else {
                    assert_eq!(Some(failure_status(&failures).as_str()), status);
                }
            }
        }
    }
}
