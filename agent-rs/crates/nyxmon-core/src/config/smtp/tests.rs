//! SMTP configuration and message against `fixtures/config_smtp.json` and
//! `fixtures/smtp_text.json` (generated from the Python executor).

use super::*;
use crate::fixture::{Field, load};
use crate::pyjson::{CmpOp, py_cmp};

#[test]
fn subjects_match_python() {
    let fixture = load(include_str!("../../../../../fixtures/smtp_text.json"));
    for case in fixture.list_field("subjects") {
        let subject = build_subject(
            case.str_field("prefix"),
            case.int_field("now"),
            case.str_field("token"),
        );
        assert_eq!(subject, case.str_field("subject"), "{case:?}");
    }
}

fn number_eq(actual: f64, expected: &PyJson) -> bool {
    py_cmp(CmpOp::Eq, &PyJson::Float(actual), expected).unwrap_or(false)
}

fn is_unsupported_address(message: &str) -> bool {
    message == unsupported_address("from_addr") || message == unsupported_address("to_addr")
}

/// Every configuration Python accepts and Rust sends gives the same
/// envelope, Message-ID and message bytes; the rest are the unsupported
/// address forms of PLAN.md 1.5 D-14.
#[test]
fn configuration_and_message_match_python() {
    let fixture = load(include_str!("../../../../../fixtures/config_smtp.json"));
    let now = fixture.int_field("now");
    let token = fixture.str_field("token");
    let (mut exact_messages, mut unsupported) = (0, 0);
    for case in fixture.list_field("cases") {
        let data = case.field("data");
        let outcome = prepare(data, now, token, |domain| format!("<1.2.3@{domain}>"));
        if case.get("exception").is_some() {
            // Python's header parser raised (IndexError): never a plain form.
            let error = outcome.err().unwrap_or_default();
            assert!(is_unsupported_address(&error), "{data:?}: {error}");
            unsupported += 1;
            continue;
        }
        if let Some(result) = case.get("result") {
            assert_eq!(result.str_field("error_type"), "configuration_error");
            let python = result.str_field("error_msg");
            if let Err(error) = &outcome
                && python == HEADER_LINE_ERROR
                && is_unsupported_address(error)
            {
                // A line boundary only in the Message-ID Python derives from
                // an unsupported sender (`a@b.c\x85`).
                unsupported += 1;
                continue;
            }
            assert_eq!(
                outcome.as_ref().err().map(String::as_str),
                Some(result.str_field("error_msg")),
                "{data:?}"
            );
            continue;
        }
        let (config, message) = match outcome {
            Ok(prepared) => prepared,
            Err(error) => {
                assert!(is_unsupported_address(&error), "{data:?}: {error}");
                unsupported += 1;
                continue;
            }
        };
        if let Some(expected) = case.get("config") {
            assert_eq!(&config.host, expected.field("host"), "{data:?}");
            assert_eq!(i64::from(config.port), expected.int_field("port"));
            let tls = match config.tls {
                SmtpTls::None => "none",
                SmtpTls::Starttls => "starttls",
                SmtpTls::Implicit => "implicit",
            };
            assert_eq!(tls, expected.str_field("tls"));
            assert_eq!(&config.username, expected.field("username"));
            assert_eq!(&config.password, expected.field("password"));
            assert_eq!(&config.password_secret, expected.field("password_secret"));
            assert_eq!(
                config.get_password().unwrap_or(&PyJson::Null),
                expected.field("get_password")
            );
            assert_eq!(config.from_addr, expected.str_field("from_addr"));
            assert_eq!(config.to_addr, expected.str_field("to_addr"));
            assert_eq!(config.subject_prefix, expected.str_field("subject_prefix"));
            assert!(
                number_eq(config.timeout, expected.field("timeout")),
                "{data:?}"
            );
            assert!(number_eq(config.retry_delay, expected.field("retry_delay")));
            match expected.field("retries") {
                PyJson::Int(n) => assert_eq!(config.retries, *n as u64),
                _ => assert_eq!(config.retries, u64::MAX),
            }
            assert_eq!(config.verify, expected.bool_field("verify"));
        } else {
            // An address corpus case: the base configuration.
            assert_eq!(
                Some(config.from_addr.as_str()),
                data.field("from_addr").as_str()
            );
            assert_eq!(
                Some(config.to_addr.as_str()),
                data.field("to_addr").as_str()
            );
        }
        assert_eq!(message.subject, case.str_field("subject"));
        assert_eq!(message.message_id, case.str_field("message_id"), "{data:?}");
        assert_eq!(
            message.envelope_from,
            case.str_field("envelope_from"),
            "{data:?}"
        );
        let recipients: Vec<&str> = case
            .list_field("recipients")
            .iter()
            .map(|r| r.as_str().expect("str"))
            .collect();
        assert_eq!(message.recipients, recipients, "{data:?}");
        // A non-ASCII or over-long subject is folded differently (P2).
        if message.subject.is_ascii() && message.subject.len() <= 78 - "Subject: ".len() {
            let flat: Vec<u8> = case
                .field("flat")
                .str_field("bytes")
                .chars()
                .map(|c| c as u8)
                .collect();
            assert_eq!(
                String::from_utf8_lossy(&message.flatten()),
                String::from_utf8_lossy(&flat),
                "{data:?}"
            );
            exact_messages += 1;
        }
    }
    assert!(exact_messages > 400, "{exact_messages}");
    assert!(unsupported > 250, "{unsupported}");
}

#[test]
fn production_addresses_are_sent_as_configured() {
    for (from, to) in [
        (
            "monitor@xn--wersdrfer-47a.de",
            "wersdoerfer.mailmon@gmail.com",
        ),
        (
            "nyxmon-local-probe@xn--wersdrfer-47a.de",
            "monitor@xn--wersdrfer-47a.de",
        ),
    ] {
        let data = crate::pyjson::loads(&format!(
            r#"{{"host": "smtp.example.com", "from_addr": "{from}", "to_addr": "{to}"}}"#
        ))
        .expect("json");
        let (_, message) = prepare(&data, 0, "t", |d| format!("<1@{d}>")).expect("plain");
        assert_eq!(message.envelope_from, from);
        assert_eq!(message.recipients, [to]);
        assert_eq!(
            message.message_id,
            format!("<1@{}>", from.split_once('@').unwrap().1)
        );
    }
}

/// PLAN.md 1.5 D-14: address forms Python accepts but the Rust agent
/// reports as `configuration_error`.
#[test]
fn unsupported_address_forms_are_configuration_errors() {
    let unsupported = [
        "J\u{fc}rgen <j@x.example>",
        "\u{e4}@b.de",
        "a@\u{a0}b.c",
        "\u{a0}alert@b.c",
        "Name <a@b.c> (ops)",
        "x@y.z (Comment)",
        "(lead) x@y.z",
        "Group: Name <j@x.example>, a@b.example;",
        "undisclosed-recipients:;",
        "\"quoted local\"@example.com",
        "a@[127.0.0.1]",
        "=?utf-8?q?J=C3=BCrgen?= <j@x.example>",
        "name <a@b.c",
        "a@b.c,",
        "a@b.c,,d@e.f",
        "a..b@c.d",
        "bad",
        "<>",
        "  spaced@example.com  ",
        "tab\t<t@example.com>",
        "Name  Two <a@b.c>",
        "\"a\\\"b\" <a@b.c>",
    ];
    let long_name = format!("{} <a@b.c>", "n".repeat(70));
    for (field, other) in [("from_addr", "to_addr"), ("to_addr", "from_addr")] {
        for value in unsupported.iter().copied().chain([long_name.as_str()]) {
            let mut map = crate::pyjson::PyDict::new();
            map.insert("host".to_owned(), PyJson::from("smtp.example.com"));
            map.insert(field.to_owned(), PyJson::from(value));
            map.insert(other.to_owned(), PyJson::from("a@b.example"));
            let outcome = prepare(&PyJson::Dict(map), 0, "t", |d| format!("<1@{d}>"));
            assert_eq!(outcome.err(), Some(unsupported_address(field)), "{value:?}");
        }
    }
    // Lists are plain for `To` only.
    let mut map = crate::pyjson::PyDict::new();
    map.insert("host".to_owned(), PyJson::from("smtp.example.com"));
    map.insert("from_addr".to_owned(), PyJson::from("a@b.c, d@e.f"));
    map.insert("to_addr".to_owned(), PyJson::from("a@b.c, d@e.f"));
    let outcome = prepare(&PyJson::Dict(map), 0, "t", |d| format!("<1@{d}>"));
    assert_eq!(outcome.err(), Some(unsupported_address("from_addr")));
}

/// The text of a header made of encoded words (whitespace between them
/// dropped, as RFC 2047 decoders do).
fn decode_encoded_words(value: &str) -> String {
    value
        .split_whitespace()
        .map(|w| {
            let w = w.strip_prefix("=?utf-8?").expect("encoded word");
            let (kind, text) = w.split_at(2);
            let text = text.strip_suffix("?=").expect("encoded word");
            match kind {
                "q?" => decode_q(text),
                "b?" => decode_b64(text),
                _ => panic!("{w}"),
            }
        })
        .collect()
}

fn decode_q(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'_' => out.push(b' '),
            b'=' => {
                out.push(u8::from_str_radix(&text[i + 1..i + 3], 16).expect("hex"));
                i += 2;
            }
            b => out.push(b),
        }
        i += 1;
    }
    String::from_utf8(out).expect("utf-8")
}

fn header_lines(folded: &str) -> Vec<&str> {
    folded
        .strip_suffix("\r\n")
        .expect("CRLF")
        .split("\r\n")
        .collect()
}

#[test]
fn subjects_that_need_encoding_decode_to_the_subject() {
    let cases = [
        "\u{e4} \u{f6} x",
        "\u{e4}  \u{f6}",
        &*format!("{} {}", "a".repeat(80), "b".repeat(80)),
        &*"a".repeat(1100),
        &*"a".repeat(77),
        &*"\u{e4}".repeat(300),
    ];
    for subject in cases {
        let folded = fold_subject(subject);
        let lines = header_lines(&folded);
        assert!(lines.iter().all(|l| l.chars().count() <= 78), "{folded}");
        let value = folded.strip_prefix("Subject: ").expect("name");
        assert_eq!(decode_encoded_words(value), subject);
    }
    assert_eq!(fold_subject("plain"), "Subject: plain\r\n");
}

fn decode_b64(text: &str) -> String {
    let table = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut bits = 0u32;
    let mut count = 0;
    let mut out = Vec::new();
    for c in text.bytes().filter(|&c| c != b'=') {
        let v = table.iter().position(|&t| t == c).expect("base64") as u32;
        bits = (bits << 6) | v;
        count += 6;
        if count >= 8 {
            count -= 8;
            out.push((bits >> count) as u8);
            bits &= (1 << count) - 1;
        }
    }
    String::from_utf8(out).expect("utf-8")
}
