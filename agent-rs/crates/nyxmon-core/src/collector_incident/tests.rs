//! `collector_incidents.json` (generated from the Python implementation by
//! the former `gen_collector.py`, removed 2026-10-25): the collector incident
//! payload and messages.

use super::*;
use crate::fixture::{Field, load};
use crate::pyjson::loads;

fn fixture() -> PyJson {
    load(include_str!(
        "../../../../fixtures/collector_incidents.json"
    ))
}

fn incident(payload_json: &str, opened_at: i64) -> CollectorIncident {
    CollectorIncident {
        incident_key: STALE_BATCH_INCIDENT_KEY.to_owned(),
        opened_at,
        last_alert_at: opened_at,
        alert_count: 1,
        payload: loads(payload_json)
            .expect("payload JSON")
            .into_dict()
            .expect("payload object"),
    }
}

/// Compare an outcome with the fixture's `{"error": …, "value": …}`.
fn assert_outcome(outcome: Result<String, PyNumError>, expected: &PyJson, context: &str) {
    match expected.field("error") {
        PyJson::Null => assert_eq!(
            outcome.as_deref().ok(),
            Some(expected.str_field("value")),
            "{context}"
        ),
        error => {
            let err = outcome.expect_err(context);
            assert_eq!(err.python_class(), error.str_field("class"), "{context}");
            assert_eq!(err.to_string(), error.str_field("message"), "{context}");
        }
    }
}

const NOW: i64 = 1_800_000_000;

#[test]
fn merged_stale_batch_payload_matches_python() {
    let fixture = fixture();
    let cases = fixture.list_field("merge");
    assert!(cases.len() > 100);
    for case in cases {
        let existing = case
            .opt_str("existing_json")
            .map(|json| incident(json, NOW - 600));
        let recovered: Vec<i64> = case
            .list_field("recovered")
            .iter()
            .map(|id| id.as_i64().expect("check id"))
            .collect();
        let outcome = merge_stale_batch_payload(existing.as_ref(), &recovered, NOW)
            .map(|payload| PyJson::Dict(payload).to_json_string().expect("dumps"));
        assert_outcome(outcome, case, &format!("{case:?}"));
    }
}

#[test]
fn incident_messages_match_python() {
    let fixture = fixture();
    for case in fixture.list_field("messages") {
        let incident = incident(case.str_field("payload_json"), case.int_field("opened_at"));
        let context = format!("{case:?}");
        assert_outcome(
            stale_batch_message(&incident),
            case.field("stale"),
            &context,
        );
        assert_outcome(
            paused_batch_message(&incident, NOW),
            case.field("paused"),
            &context,
        );
    }
}

#[test]
fn paused_payload_samples_at_most_twenty_ids() {
    let ids: Vec<i64> = (1..=25).collect();
    let payload = PyJson::Dict(paused_batch_payload(&ids));
    assert_eq!(
        payload.to_json_string().expect("dumps"),
        "{\"incident_type\": \"collector_execution_paused\", \"batch_size\": 25, \"check_ids\": \
         [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]}"
    );
}
