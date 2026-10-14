//! `fixtures/ping_text.json` (command lines, parsers, statistics) and the
//! character classes of `fixtures/ping_unicode.json`.

use super::*;
use crate::fixture::{Field, hex_bytes, load};

fn fixture() -> PyJson {
    load(include_str!("../../../../fixtures/ping_text.json"))
}

fn unicode() -> PyJson {
    load(include_str!("../../../../fixtures/ping_unicode.json"))
}

fn float(value: &PyJson) -> f64 {
    match value {
        PyJson::Float(number) => *number,
        PyJson::Int(number) => *number as f64,
        other => panic!("not a number: {other:?}"),
    }
}

fn floats(value: &PyJson) -> Vec<f64> {
    value.as_list().expect("a list").iter().map(float).collect()
}

fn strings(value: &PyJson) -> Vec<String> {
    value
        .as_list()
        .expect("a list")
        .iter()
        .map(|item| item.as_str().expect("a string").to_owned())
        .collect()
}

#[test]
fn constants_match() {
    let fixture = fixture();
    let constants = fixture.field("constants");
    assert_eq!(
        float(constants.field("process_grace_seconds")),
        PROCESS_GRACE_SECONDS
    );
    assert_eq!(
        strings(constants.field("fallback_ping_paths")),
        FALLBACK_PING_PATHS
    );
    assert_eq!(
        strings(constants.field("fallback_ping6_paths")),
        FALLBACK_PING6_PATHS
    );
    assert_eq!(
        constants.int_field("max_error_detail"),
        MAX_ERROR_DETAIL as i64
    );
    assert_eq!(constants.str_field("permission_hint"), PERMISSION_HINT);
}

#[test]
fn command_lines_match() {
    let fixture = fixture();
    let cases = fixture.list_field("commands");
    assert!(cases.len() > 1000);
    for case in cases {
        let platform = case.str_field("platform");
        let address = case.str_field("address");
        let timeout = float(case.field("timeout"));
        let ping6 = case.opt_str("ping6");
        let command = build_ping_command(platform, address, timeout, "/bin/ping", ping6);
        assert_eq!(
            command,
            Ok(strings(case.field("command"))),
            "{platform} {address} {timeout} {ping6:?}"
        );
    }
    assert_eq!(
        build_ping_command("linux", "host.example", 1.0, "/bin/ping", None),
        Err(PyException::VALUE_ERROR)
    );
}

#[test]
fn rtt_parsing_matches() {
    let fixture = fixture();
    for case in fixture.list_field("rtt") {
        let output = case.str_field("output");
        let address = case.opt_str("address");
        let expected = match case.field("rtt") {
            PyJson::Null => None,
            value => Some(float(value).to_bits()),
        };
        assert_eq!(
            parse_rtt_ms(output, address).map(f64::to_bits),
            expected,
            "{output:?} / {address:?}"
        );
    }
}

#[test]
fn error_details_match() {
    let fixture = fixture();
    for case in fixture.list_field("error_detail") {
        let stdout = case.str_field("stdout");
        let stderr = case.str_field("stderr");
        assert_eq!(
            error_detail(stdout, stderr),
            case.str_field("detail"),
            "{stdout:?} / {stderr:?}"
        );
    }
}

#[test]
fn permission_markers_match() {
    let fixture = fixture();
    for case in fixture.list_field("permission") {
        let output = case.str_field("output");
        assert_eq!(
            is_permission_failure(output),
            case.bool_field("permission"),
            "{output:?}"
        );
    }
}

#[test]
fn output_decoding_matches() {
    let fixture = fixture();
    for case in fixture.list_field("decode") {
        let data = hex_bytes(case.str_field("hex"));
        assert_eq!(
            decode_output(&data),
            case.str_field("text"),
            "{}",
            case.str_field("hex")
        );
    }
}

#[test]
fn packet_stats_match() {
    let fixture = fixture();
    for case in fixture.list_field("packet_stats") {
        let stats = packet_stats(case.int_field("sent"), case.int_field("received"));
        assert_eq!(
            PyJson::Dict(stats).to_json_string().expect("serialisable"),
            case.str_field("stats_json"),
            "{case:?}"
        );
    }
}

#[test]
fn rtt_statistics_match() {
    let fixture = fixture();
    for case in fixture.list_field("rtt_stats") {
        let rtts = floats(case.field("rtts"));
        let (min, max, avg) = rtt_stats(&rtts);
        let bits = |key: &str| float(case.field(key)).to_bits();
        assert_eq!(min.to_bits(), bits("min"), "{rtts:?}");
        assert_eq!(max.to_bits(), bits("max"), "{rtts:?}");
        assert_eq!(avg.to_bits(), bits("avg"), "{rtts:?}");
    }
    assert_eq!(py_sum(&[]), 0.0);
}

fn code_points(value: &PyJson) -> Vec<char> {
    value
        .as_list()
        .expect("a list")
        .iter()
        .map(|code| char::from_u32(code.as_i64().expect("int") as u32).expect("a char"))
        .collect()
}

fn in_generated_ranges(ranges: &PyJson, c: char) -> bool {
    ranges.as_list().expect("ranges").iter().any(|range| {
        let bounds = range.as_list().expect("a range");
        let code = i64::from(c as u32);
        bounds[0].as_i64().expect("start") <= code && code <= bounds[1].as_i64().expect("end")
    })
}

#[test]
fn character_classes_follow_the_oracle() {
    let table = unicode();
    let space = table.field("space");
    let word = table.field("word");
    for c in (0..=0x10ffff_u32).filter_map(char::from_u32) {
        assert_eq!(py_isspace(c), in_generated_ranges(space, c), "\\s {c:?}");
        if c.is_ascii() {
            // Other characters are looked up in the same table.
            assert_eq!(is_word(c), in_generated_ranges(word, c), "\\w {c:?}");
        }
    }
    for (letter, members) in table.field("ignorecase").as_dict().expect("letters") {
        let letter = letter.chars().next().expect("a letter");
        let members = code_points(members);
        for c in (0..=0x10ffff_u32).filter_map(char::from_u32) {
            assert_eq!(
                matches_ignorecase(c, letter),
                members.contains(&c),
                "{letter} {c:?}"
            );
        }
    }
    let separators = code_points(table.field("line_separators"));
    for c in (0..=0x10ffff_u32).filter_map(char::from_u32) {
        let text = format!("a{c}b");
        assert_eq!(
            py_splitlines(&text).len() == 2,
            separators.contains(&c),
            "{c:?}"
        );
    }
}

#[test]
fn splitlines_follows_python() {
    assert_eq!(py_splitlines(""), Vec::<&str>::new());
    assert_eq!(py_splitlines("\n"), vec![""]);
    assert_eq!(py_splitlines("a\r\nb\n\nc"), vec!["a", "b", "", "c"]);
    assert_eq!(py_splitlines("a\r\rb\n"), vec!["a", "", "b"]);
    assert_eq!(py_splitlines("\u{2028}x\u{85}"), vec!["", "x"]);
}
