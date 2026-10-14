//! `fixtures/config_tcp.json`: `TcpCheckConfig.from_dict` + `validate` and
//! `_resolve_host`, as `TcpCheckExecutor.execute` runs them before any I/O.

use super::*;
use crate::fixture::{Field, load};
use crate::pyjson::loads;

fn fixture() -> PyJson {
    load(include_str!("../../../../../fixtures/config_tcp.json"))
}

fn float(value: &PyJson) -> f64 {
    match value {
        PyJson::Float(number) => *number,
        other => panic!("not a float: {other:?}"),
    }
}

#[test]
fn every_config_case_matches() {
    let fixture = fixture();
    let cases = fixture.list_field("cases");
    assert!(cases.len() > 100);
    for case in cases {
        let data = loads(case.str_field("data_json")).expect("valid data JSON");
        let url = case.str_field("url");
        let expected = case.field("outcome");
        let context = format!("{} / {url:?}", case.str_field("data_json"));
        let outcome = TcpConfig::from_data(&data);
        match expected.str_field("kind") {
            "configuration_error" if expected.str_field("error_msg") == HOST_REQUIRED => {
                let ConfigOutcome::Ok(config) = outcome else {
                    panic!("{context}: expected a valid config, got {outcome:?}");
                };
                assert_eq!(config.target_host(url), Ok(None), "{context}");
            }
            "configuration_error" => {
                assert_eq!(
                    outcome,
                    ConfigOutcome::ConfigError(expected.str_field("error_msg").to_owned()),
                    "{context}"
                );
            }
            "exception" => match outcome {
                ConfigOutcome::PyException(exc) => {
                    assert_eq!(exc.0, expected.str_field("class"), "{context}");
                }
                ConfigOutcome::Ok(config) => {
                    let target = config.target_host(url);
                    assert_eq!(expected.str_field("class"), "ValueError", "{context}");
                    assert_eq!(
                        target.map(|_| ()),
                        Err(PyException::VALUE_ERROR),
                        "{context}"
                    );
                }
                other => panic!("{context}: expected an exception, got {other:?}"),
            },
            "ready" => {
                let ConfigOutcome::Ok(config) = outcome else {
                    panic!("{context}: expected ready, got {outcome:?}");
                };
                let fields = expected.field("config");
                assert_eq!(config.port, fields.int_field("port"), "{context}");
                assert_eq!(&config.host, fields.field("host"), "{context}");
                assert_eq!(config.tls_mode.as_str(), fields.str_field("tls_mode"));
                assert_eq!(
                    config.connect_timeout.to_bits(),
                    float(fields.field("connect_timeout")).to_bits(),
                    "{context}"
                );
                assert_eq!(
                    config.tls_handshake_timeout.to_bits(),
                    float(fields.field("tls_handshake_timeout")).to_bits()
                );
                assert_eq!(
                    config.retry_delay.to_bits(),
                    float(fields.field("retry_delay")).to_bits(),
                    "{context}"
                );
                assert_eq!(config.retries, fields.int_field("retries"));
                assert_eq!(
                    config.check_cert_expiry,
                    fields.bool_field("check_cert_expiry")
                );
                assert_eq!(config.min_cert_days, fields.int_field("min_cert_days"));
                assert_eq!(&config.sni, fields.field("sni"), "{context}");
                assert_eq!(
                    &config.starttls_command,
                    fields.field("starttls_command"),
                    "{context}"
                );
                assert_eq!(
                    config.starttls_protocol.as_str(),
                    fields.str_field("starttls_protocol")
                );
                assert_eq!(
                    config.starttls_read_greeting,
                    fields.bool_field("starttls_read_greeting")
                );
                assert_eq!(config.verify, fields.bool_field("verify"));
                assert_eq!(config.attempts(), expected.int_field("attempts"));
                assert_eq!(
                    config.target_host(url),
                    Ok(Some(expected.field("target").clone())),
                    "{context}"
                );
            }
            other => panic!("unknown outcome kind {other}"),
        }
    }
}

#[test]
fn str_of_odd_hosts_matches_python() {
    let fixture = fixture();
    for case in fixture.list_field("str_values") {
        let value = loads(case.str_field("value_json")).expect("valid JSON");
        assert_eq!(py_str(&value), case.str_field("str"), "{value:?}");
    }
}

#[test]
fn ip_address_of_integer_hosts_matches_python() {
    let fixture = fixture();
    for case in fixture.list_field("ip_from_int") {
        let value = loads(case.str_field("value_json")).expect("valid JSON");
        let expected = case.opt_str("literal");
        match connect_target(&value) {
            ConnectTarget::Address(addr) => {
                assert_eq!(Some(addr.to_string().as_str()), expected, "{value:?}");
            }
            ConnectTarget::Name(name) => {
                assert_eq!(expected, None, "{value:?}");
                assert_eq!(name, py_str(&value));
            }
            ConnectTarget::ScopedAddress(_) => panic!("integers have no zone"),
        }
    }
}

#[test]
fn connect_targets() {
    let name = |text: &str| ConnectTarget::Name(text.to_owned());
    assert_eq!(
        connect_target(&PyJson::Str("192.0.2.1".to_owned())),
        ConnectTarget::Address("192.0.2.1".parse().unwrap())
    );
    assert_eq!(
        connect_target(&PyJson::Str("fe80::1%lo0".to_owned())),
        ConnectTarget::ScopedAddress("fe80::1%lo0".to_owned())
    );
    assert_eq!(connect_target(&PyJson::Float(1.5)), name("1.5"));
    assert_eq!(connect_target(&loads("[\"a\"]").unwrap()), name("['a']"));
}
