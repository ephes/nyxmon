//! IMAP configuration and helpers against `fixtures/config_imap.json` and
//! `fixtures/imap_text.json` (generated from the Python code).

use super::*;
use crate::fixture::{Field, load};
use crate::pyjson::{CmpOp, py_cmp};

/// The same JSON value (NaN included).
fn same(actual: &PyJson, expected: &PyJson) -> bool {
    actual.to_json_string().ok() == expected.to_json_string().ok()
}

fn tls_name(mode: ImapTls) -> &'static str {
    match mode {
        ImapTls::Implicit => "implicit",
        ImapTls::Starttls => "starttls",
        ImapTls::None => "none",
    }
}

/// Every corpus value gives Python's outcome; the only exception is a
/// truthy list or object `host` (PLAN.md 1.5 D-18).
#[test]
fn configuration_matches_python() {
    let fixture = load(include_str!("../../../../../fixtures/config_imap.json"));
    let (mut accepted, mut unsupported) = (0, 0);
    for case in fixture.list_field("cases") {
        let data = case.field("data");
        let outcome = ImapConfig::from_data(data);
        if let Some(error) = case.get("error") {
            let expected = match error.str_field("class") {
                "ValueError" => ConfigError::Invalid(error.str_field("message").to_owned()),
                class => ConfigError::Exception(PyException(match class {
                    "TypeError" => "TypeError",
                    "AttributeError" => "AttributeError",
                    other => panic!("unexpected exception class {other}"),
                })),
            };
            assert_eq!(outcome, Err(expected), "{data:?}");
            continue;
        }
        let python = case.field("config");
        if matches!(python.field("host"), PyJson::List(_) | PyJson::Dict(_)) {
            assert_eq!(
                outcome,
                Err(ConfigError::invalid(UNSUPPORTED_HOST)),
                "{data:?}"
            );
            unsupported += 1;
            continue;
        }
        let config = outcome.unwrap_or_else(|err| panic!("{data:?}: {err:?}"));
        for (key, value) in [
            ("host", &config.host),
            ("username", &config.username),
            ("password", &config.password),
            ("password_secret", &config.password_secret),
            ("search_subject", &config.search_subject),
            ("folder", &config.folder),
        ] {
            assert!(same(value, python.field(key)), "{key} of {data:?}");
        }
        for (key, value) in [
            ("port", PyJson::Int(i64::from(config.port))),
            ("max_age_minutes", PyJson::Int(config.max_age_minutes)),
            ("retries", PyJson::Int(i64::from(config.retries))),
            (
                "delete_after_check",
                PyJson::Bool(config.delete_after_check),
            ),
            ("verify", PyJson::Bool(config.verify)),
            ("tls_mode", PyJson::from(tls_name(config.tls_mode))),
            (
                "no_recent_message_severity",
                PyJson::from(config.severity()),
            ),
        ] {
            assert!(same(&value, python.field(key)), "{key} of {data:?}");
        }
        for (key, value) in [
            ("timeout", config.timeout),
            ("retry_delay", config.retry_delay),
        ] {
            let ok = py_cmp(CmpOp::Eq, &PyJson::Float(value), python.field(key));
            assert_eq!(ok, Ok(true), "{key} of {data:?}");
        }
        accepted += 1;
    }
    assert!(
        accepted > 300 && unsupported > 0,
        "{accepted} {unsupported}"
    );
}

#[test]
fn hosts_are_normalized_like_python() {
    let fixture = load(include_str!("../../../../../fixtures/imap_text.json"));
    for case in fixture.list_field("hosts") {
        let host = case.str_field("host");
        let expected = match case.get("error") {
            Some(error) => Err(error.as_str().unwrap_or_default().to_owned()),
            None => Ok(case.str_field("normalized").to_owned()),
        };
        assert_eq!(normalize_host(host), expected, "{host:?}");
    }
    let number = |value: PyJson| ImapConfig {
        host: value,
        ..ImapConfig::from_data(&crate::pyjson::loads(BASE).unwrap()).unwrap()
    };
    assert_eq!(
        number(PyJson::Int(5)).session_host(),
        Err("argument of type 'int' is not iterable".to_owned())
    );
    assert_eq!(
        number(PyJson::Bool(true)).session_host(),
        Err("argument of type 'bool' is not iterable".to_owned())
    );
}

const BASE: &str = r#"{"host": "h", "username": "u", "password": "p", "search_subject": "s"}"#;

fn latin1(text: &str) -> Vec<u8> {
    text.chars().map(|c| c as u8).collect()
}

/// `_fetch_internaldate(uid="20")` for one response line.
#[test]
fn internaldates_match_python() {
    let fixture = load(include_str!("../../../../../fixtures/imap_text.json"));
    for case in fixture.list_field("internaldates") {
        let line = latin1(case.str_field("line"));
        let actual = if fetch_uid(&line) == Some(b"20".as_slice()) {
            parse_internaldate(&line)
        } else {
            InternalDate::Missing
        };
        let expected = match (case.get("error"), case.get("iso")) {
            (Some(error), _) => InternalDate::Error(error.as_str().unwrap().to_owned()),
            (None, Some(PyJson::Null)) => InternalDate::Missing,
            (None, Some(iso)) => {
                let InternalDate::At(at) = actual else {
                    panic!("{line:?}: {actual:?}");
                };
                assert_eq!(iso_utc(at), iso.as_str().unwrap(), "{line:?}");
                continue;
            }
            (None, None) => panic!("malformed case"),
        };
        assert_eq!(actual, expected, "{line:?}");
    }
}

#[test]
fn search_results_split_like_python() {
    let fixture = load(include_str!("../../../../../fixtures/imap_text.json"));
    for case in fixture.list_field("searches") {
        let data = latin1(case.str_field("data"));
        let expected = match case.get("error") {
            Some(error) => Err(error.as_str().unwrap().to_owned()),
            None => Ok(case
                .list_field("ids")
                .iter()
                .map(|id| id.as_str().unwrap().to_owned())
                .collect::<Vec<_>>()),
        };
        assert_eq!(search_ids(&data), expected, "{data:?}");
    }
}

#[test]
fn out_of_range_dates_are_errors() {
    let line = |date: &str| format!("1 (UID 20 INTERNALDATE \"{date}\")").into_bytes();
    assert_eq!(
        parse_internaldate(&line("31-Dec-9999 23:59:59 -0100")),
        InternalDate::Error("year 10000 is out of range".to_owned())
    );
    assert_eq!(
        parse_internaldate(&line("01-Jan-0001 00:00:00 +0100")),
        InternalDate::Error("year 0 is out of range".to_owned())
    );
    assert_eq!(
        iso_utc(-2_208_988_800),
        "1900-01-01T00:00:00+00:00",
        "a date before 1970"
    );
    assert_eq!(
        utf8_decode_error(b"5 \xe2"),
        Some(
            "'utf-8' codec can't decode byte 0xe2 in position 2: unexpected end of data".to_owned()
        )
    );
}
