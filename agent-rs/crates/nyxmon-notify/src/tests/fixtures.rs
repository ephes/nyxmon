//! The pure notifier functions against the Python fixtures.

use num_bigint::BigInt;
use nyxmon_core::model::ResultStatus;
use nyxmon_core::pyjson::{PyJson, loads, py_int};

use super::{check_of, field, int_field, list_field, load, result_of, str_field};
use crate::markdown::{
    alert_body, append_ticket_notice, escape_markdown_link_url, escape_markdown_v2,
    markdown_v2_to_plain_text, ticket_requested,
};
use crate::opsgate::{TicketOutcome, build_prompt, build_ticket_payload, encode_payload, task_ref};
use crate::sanitize::{sanitize_untrusted_data, sanitize_untrusted_text, truncate_chars};

fn telegram() -> PyJson {
    load(include_str!("../../../../fixtures/telegram.json"))
}

fn opsgate() -> PyJson {
    load(include_str!("../../../../fixtures/opsgate.json"))
}

fn io_cases(fixture: &PyJson, key: &str, f: impl Fn(&str) -> String) {
    let cases = list_field(fixture, key);
    assert!(!cases.is_empty(), "{key} has cases");
    for case in cases {
        let input = str_field(case, "input");
        assert_eq!(f(input), str_field(case, "output"), "{key}: {input:?}");
    }
}

#[test]
fn markdown_helpers_match_python() {
    let fixture = telegram();
    io_cases(&fixture, "escape", escape_markdown_v2);
    io_cases(&fixture, "plain_text", markdown_v2_to_plain_text);
    io_cases(&fixture, "link_url", escape_markdown_link_url);
    for case in list_field(&fixture, "truncate") {
        let max = usize::try_from(int_field(case, "max_chars")).expect("non-negative");
        assert_eq!(
            truncate_chars(str_field(case, "input"), max),
            str_field(case, "output")
        );
    }
}

fn ticket_of(spec: &PyJson) -> Option<TicketOutcome> {
    match spec {
        PyJson::Null => None,
        spec => Some(match str_field(spec, "status") {
            "created" => TicketOutcome::Created(str_field(spec, "ticket_id").to_owned()),
            "duplicate" => TicketOutcome::Duplicate,
            "error" => TicketOutcome::Error,
            other => panic!("unknown ticket status {other}"),
        }),
    }
}

#[test]
fn alert_messages_match_python() {
    let fixture = telegram();
    let cases = list_field(&fixture, "messages");
    assert!(cases.len() > 50);
    for case in cases {
        let label = str_field(case, "label");
        let check = check_of(field(case, "check"));
        let result = result_of(field(case, "result"));
        let include = field(case, "include_warnings").as_bool().expect("bool");
        let requested = ticket_requested(&check, &result, include);
        assert_eq!(
            Some(requested),
            field(case, "ticket_requested").as_bool(),
            "{label}"
        );
        let mut text = alert_body(&check, &result);
        if requested && let Some(ticket) = ticket_of(field(case, "ticket")) {
            append_ticket_notice(&mut text, &ticket, "https://approve.example/base");
        }
        assert_eq!(text, str_field(case, "text"), "{label}");
        assert_eq!(
            Some(result.status == ResultStatus::Error),
            field(case, "high_priority").as_bool(),
            "{label}"
        );
    }
}

#[test]
fn sanitize_matches_python() {
    let fixture = opsgate();
    for case in list_field(&fixture, "sanitize") {
        let input = loads(str_field(case, "input")).expect("JSON input");
        let output = sanitize_untrusted_data(&input)
            .to_json_string()
            .expect("dumps");
        assert_eq!(output, str_field(case, "output"), "{input:?}");
    }
    for case in list_field(&fixture, "sanitize_text") {
        let max = match field(case, "max_chars") {
            PyJson::Null => None,
            value => Some(usize::try_from(value.as_i64().expect("int")).expect("non-negative")),
        };
        assert_eq!(
            sanitize_untrusted_text(str_field(case, "input"), max),
            str_field(case, "output")
        );
    }
}

#[test]
fn prompts_and_task_refs_match_python() {
    let fixture = opsgate();
    let nonce = str_field(&fixture, "nonce");
    let cases = list_field(&fixture, "prompts");
    assert!(cases.len() >= 90);
    for case in cases {
        let check = check_of(field(case, "check"));
        let result = result_of(field(case, "result"));
        assert_eq!(task_ref(&check, &result), str_field(case, "task_ref"));
        assert_eq!(
            build_prompt(&check, &result, nonce),
            str_field(case, "prompt"),
            "{case:?}"
        );
    }
}

#[test]
fn ticket_payloads_match_python() {
    let fixture = opsgate();
    let nonce = str_field(&fixture, "nonce");
    let cases = list_field(&fixture, "payloads");
    let (mut bodies, mut raised, mut encode_errors) = (0, 0, 0);
    for case in cases {
        let check = check_of(field(case, "check"));
        let result = result_of(field(case, "result"));
        let expires = match field(case, "expires_seconds") {
            PyJson::Null => BigInt::from(14_400),
            value => py_int(value.as_str().expect("string")).expect("integer"),
        };
        let now = int_field(case, "now_micros");
        assert_eq!(task_ref(&check, &result), str_field(case, "task_ref"));
        match build_ticket_payload(&check, &result, now, &expires, nonce) {
            Err(err) => {
                assert_eq!(err.class, str_field(case, "raised"), "{case:?}");
                raised += 1;
            }
            Ok(payload) => match encode_payload(&payload) {
                Ok(body) => {
                    assert_eq!(body, str_field(case, "body"), "{case:?}");
                    bodies += 1;
                }
                Err(message) => {
                    assert_eq!(message, str_field(case, "encode_error"));
                    assert_eq!(
                        payload.get("expires_at").and_then(PyJson::as_str),
                        Some(str_field(case, "expires_at"))
                    );
                    encode_errors += 1;
                }
            },
        }
    }
    assert!(bodies > 20 && raised >= 4 && encode_errors >= 1);
}

#[test]
fn prompt_boundary_is_unpredictable() {
    let first = crate::telegram::random_nonce();
    let second = crate::telegram::random_nonce();
    assert_ne!(first, second);
    for nonce in [&first, &second] {
        assert_eq!(nonce.len(), 16);
        assert!(
            nonce
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
        );
    }
}
