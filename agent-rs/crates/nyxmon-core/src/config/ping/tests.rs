//! `fixtures/config_ping.json`: `PingCheckConfig.from_dict` + `validate`
//! and the target host, as `PingCheckExecutor.execute` runs them before the
//! name resolution.

use super::*;
use crate::fixture::{Field, load};
use crate::pyjson::loads;

fn error_json(error_type: &str, message: &str, extra: PyDict) -> String {
    let mut data = PyDict::new();
    data.insert("error_type".to_owned(), PyJson::Str(error_type.to_owned()));
    data.insert("error_msg".to_owned(), PyJson::Str(message.to_owned()));
    data.extend(extra);
    PyJson::Dict(data).to_json_string().expect("serialisable")
}

#[test]
fn every_config_case_matches() {
    let fixture = load(include_str!("../../../../../fixtures/config_ping.json"));
    let cases = fixture.list_field("cases");
    assert!(cases.len() > 90);
    for case in cases {
        let data = loads(case.str_field("data_json")).expect("valid data JSON");
        let url = case.str_field("url");
        let expected = case.field("outcome");
        let context = format!("{} / {url:?}", case.str_field("data_json"));
        let actual = match PingConfig::from_data(&data) {
            ConfigOutcome::ConfigError(message) => {
                error_json("configuration_error", &message, PyDict::new())
            }
            ConfigOutcome::Ok(config) => match config.target_host(url) {
                None => error_json("configuration_error", TARGET_REQUIRED, PyDict::new()),
                Some(host) => {
                    assert_eq!(expected.str_field("kind"), "ready", "{context}");
                    assert_eq!(host, expected.str_field("host"), "{context}");
                    error_json("dns_error", "stop", config.base_data(&host))
                }
            },
            ConfigOutcome::PyException(exc) => panic!("{context}: unexpected {exc:?}"),
        };
        assert_eq!(actual, expected.str_field("result_json"), "{context}");
    }
}

#[test]
fn null_data_reads_as_an_empty_object() {
    // PLAN.md D-11: the store reads a `null` column as `{}`.
    let ConfigOutcome::Ok(config) = PingConfig::from_data(&PyJson::Null) else {
        panic!("null data must use the defaults");
    };
    assert_eq!(
        config,
        PingConfig {
            timeout: 5.0,
            count: 3,
            interval: 1.0,
            host: None
        }
    );
}

#[test]
fn unicode_digits_are_not_numbers() {
    // PLAN.md D-6: Python's `float("٢")` is 2.0; Rust rejects it.
    let data = loads(r#"{"timeout": "٢"}"#).expect("valid JSON");
    assert_eq!(
        PingConfig::from_data(&data),
        ConfigOutcome::ConfigError("timeout must be a number".to_owned())
    );
}
