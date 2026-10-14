//! `config_dns.json` (`cases`) and the pure parts of `dns_text.json`.

use super::*;
use crate::fixture::{Field, load};

fn number(value: &PyJson) -> f64 {
    match value {
        PyJson::Int(n) => *n as f64,
        PyJson::Float(f) => *f,
        other => panic!("not a number: {other:?}"),
    }
}

#[test]
fn config_cases_match_python() {
    let fixture = load(include_str!("../../../../../fixtures/config_dns.json"));
    let cases = fixture.list_field("cases");
    assert!(cases.len() > 80);
    for case in cases {
        let data = case.field("data");
        let outcome = case.field("outcome");
        let label = format!("{data:?}");
        match DnsConfig::from_data(data) {
            Ok(config) => {
                let ok = outcome
                    .get("ok")
                    .unwrap_or_else(|| panic!("{label}: expected {outcome:?}, got {config:?}"));
                let expected_ips: Vec<&str> = ok
                    .list_field("expected_ips")
                    .iter()
                    .map(|v| v.as_str().unwrap())
                    .collect();
                assert_eq!(config.expected_ips, expected_ips, "{label}");
                assert_eq!(
                    config.query_type.as_str(),
                    ok.str_field("query_type"),
                    "{label}"
                );
                assert_eq!(config.timeout, number(ok.field("timeout")), "{label}");
                assert_eq!(&config.dns_server, ok.field("dns_server"), "{label}");
                assert_eq!(&config.source_ip, ok.field("source_ip"), "{label}");
            }
            Err(DnsConfigError::Value(message)) => {
                assert_eq!(
                    outcome.get("error").and_then(PyJson::as_str),
                    Some(message.as_str()),
                    "{label}"
                );
            }
            Err(DnsConfigError::Exception { class, message }) => {
                let exception = outcome
                    .get("exception")
                    .unwrap_or_else(|| panic!("{label}: expected {outcome:?}, got {class}"));
                assert_eq!(exception.str_field("class"), class, "{label}");
                assert_eq!(exception.str_field("message"), message, "{label}");
            }
        }
    }
}

#[test]
fn text_helpers_match_python() {
    let fixture = load(include_str!("../../../../../fixtures/dns_text.json"));
    for case in fixture.list_field("normalize") {
        let value = case.str_field("value");
        assert_eq!(
            normalize_dns_value(value),
            case.str_field("result"),
            "{value:?}"
        );
    }
    for case in fixture.list_field("expected_ip_error") {
        let value = case.str_field("value");
        let query_type = QueryType::parse(case.str_field("query_type")).unwrap();
        assert_eq!(
            expected_ip_error(value, query_type).as_deref(),
            case.opt_str("result"),
            "{value:?} {query_type:?}"
        );
    }
    for case in fixture.list_field("ip_match") {
        let strings = |key: &str| -> Vec<String> {
            case.list_field(key)
                .iter()
                .map(|v| v.as_str().unwrap().to_owned())
                .collect()
        };
        assert_eq!(
            check_ip_match(
                &strings("resolved"),
                &strings("expected"),
                case.bool_field("compare_addresses")
            ),
            case.bool_field("result"),
            "{case:?}"
        );
    }
}
