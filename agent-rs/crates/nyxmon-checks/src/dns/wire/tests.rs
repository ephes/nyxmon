//! Name text, IPv6 text and UTF-8 errors against `fixtures/dns_text.json`,
//! plus unit tests of the wire parser.

use nyxmon_core::pyjson::{PyJson, loads};

use super::*;

fn fixture() -> PyJson {
    loads(include_str!("../../../../../fixtures/dns_text.json")).unwrap()
}

fn hex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
        .collect()
}

fn hex_of(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

#[test]
fn query_names_match_dnspython() {
    let fixture = fixture();
    let cases = fixture.get("names").unwrap().as_list().unwrap();
    assert!(cases.len() > 25);
    for case in cases {
        let input = case.get("input").unwrap().as_str().unwrap();
        match name_from_text(input) {
            Ok(name) => {
                assert_eq!(
                    Some(name.to_text().as_str()),
                    case.get("text").and_then(PyJson::as_str),
                    "{input:?}"
                );
                let mut wire = Vec::new();
                name.write_wire(&mut wire);
                assert_eq!(
                    Some(hex_of(&wire).as_str()),
                    case.get("wire_hex").and_then(PyJson::as_str),
                    "{input:?}"
                );
            }
            Err(err) => {
                let exception = case
                    .get("exception")
                    .unwrap_or_else(|| panic!("{input:?}: {err:?} but Python accepts it"));
                assert_eq!(
                    exception.get("message").and_then(PyJson::as_str),
                    Some(err.to_string().as_str()),
                    "{input:?}"
                );
            }
        }
    }
}

#[test]
fn non_ascii_names_fail_closed() {
    // PLAN.md D-16: dnspython would IDNA-encode; the Rust agent refuses.
    assert_eq!(
        name_from_text("wersdörfer.de"),
        Err(NameTextError::NonAscii)
    );
}

#[test]
fn ipv6_text_matches_dnspython() {
    let fixture = fixture();
    for case in fixture.get("ipv6").unwrap().as_list().unwrap() {
        let bytes: [u8; 16] = hex(case.get("hex").unwrap().as_str().unwrap())
            .try_into()
            .unwrap();
        assert_eq!(
            ipv6_text(&bytes),
            case.get("text").unwrap().as_str().unwrap(),
            "{case:?}"
        );
    }
}

#[test]
fn utf8_errors_match_cpython() {
    let fixture = fixture();
    for case in fixture.get("utf8_decode").unwrap().as_list().unwrap() {
        let bytes = hex(case.get("hex").unwrap().as_str().unwrap());
        let expected = match case.get("text") {
            Some(text) => Ok(text.as_str().unwrap().to_owned()),
            None => Err(case.get("error").unwrap().as_str().unwrap().to_owned()),
        };
        assert_eq!(py_utf8_decode(&bytes), expected, "{case:?}");
    }
}

fn header(id: u16, flags: u16, counts: [u16; 4]) -> Vec<u8> {
    let mut out = id.to_be_bytes().to_vec();
    out.extend(flags.to_be_bytes());
    for count in counts {
        out.extend(count.to_be_bytes());
    }
    out
}

fn question_wire() -> Vec<u8> {
    let mut out = Vec::new();
    name_from_text("a.test").unwrap().write_wire(&mut out);
    out.extend(1u16.to_be_bytes());
    out.extend(1u16.to_be_bytes());
    out
}

#[test]
fn compression_pointers_must_point_backwards() {
    let mut wire = header(7, FLAG_QR | FLAG_RD, [1, 1, 0, 0]);
    wire.extend(question_wire());
    // Owner: pointer to the question name at offset 12.
    let mut answer = vec![0xc0, 12];
    answer.extend(1u16.to_be_bytes());
    answer.extend(1u16.to_be_bytes());
    answer.extend(300u32.to_be_bytes());
    answer.extend(4u16.to_be_bytes());
    answer.extend([192, 0, 2, 1]);
    let mut good = wire.clone();
    good.extend(&answer);
    let message = parse_message(&good).unwrap();
    assert_eq!(message.answers[0].name.to_text(), "a.test.");
    let rrset = resolve_chaining(&message).unwrap().unwrap();
    assert_eq!(record_texts(&rrset).unwrap(), ["192.0.2.1"]);

    // A pointer to itself (or forwards) is rejected.
    let mut bad = wire.clone();
    let self_offset = u8::try_from(bad.len()).unwrap();
    bad.extend([0xc0, self_offset]);
    bad.extend(&answer[2..]);
    assert!(parse_message(&bad).is_err());

    // Trailing bytes are rejected.
    let mut trailing = good.clone();
    trailing.push(0);
    assert!(parse_message(&trailing).is_err());

    // A wrong A length is rejected.
    let mut short = wire;
    short.extend(&answer[..10]);
    short.extend(3u16.to_be_bytes());
    short.extend([192, 0, 2]);
    assert!(parse_message(&short).is_err());
}

#[test]
fn responses_are_matched_like_is_response() {
    let question = Question {
        name: name_from_text("a.test").unwrap(),
        rtype: 1,
        class: CLASS_IN,
    };
    let mut upper = header(9, FLAG_QR, [1, 0, 0, 0]);
    name_from_text("A.TEST").unwrap().write_wire(&mut upper);
    upper.extend([0, 1, 0, 1]);
    let message = parse_message(&upper).unwrap();
    assert!(is_response(9, &question, &message));
    assert!(!is_response(10, &question, &message));
    // SERVFAIL without a question still answers the query.
    let servfail = parse_message(&header(9, FLAG_QR | RCODE_SERVFAIL, [0; 4])).unwrap();
    assert!(is_response(9, &question, &servfail));
    let noerror = parse_message(&header(9, FLAG_QR, [0; 4])).unwrap();
    assert!(!is_response(9, &question, &noerror));
}

#[test]
fn queries_carry_rd_and_optional_edns() {
    let name = name_from_text("a.test").unwrap();
    let plain = build_query(0x1234, &name, QueryType::Txt, None);
    let parsed = parse_message(&plain).unwrap();
    assert_eq!(parsed.id, 0x1234);
    assert_eq!(parsed.flags, FLAG_RD);
    assert_eq!(parsed.questions[0].rtype, 16);
    assert_eq!(parsed.edns_flags, None);
    let edns = parse_message(&build_query(1, &name, QueryType::A, Some(1232))).unwrap();
    assert_eq!(edns.edns_flags, Some(0));
    assert_eq!(edns.edns_payload, 1232);
}

#[test]
fn cname_chains_are_bounded() {
    // 16 CNAMEs a0 -> a1 -> ... -> a16: ChainTooLong.
    let mut wire = header(1, FLAG_QR, [1, 17, 0, 0]);
    name_from_text("a0.test").unwrap().write_wire(&mut wire);
    wire.extend([0, 1, 0, 1]);
    for i in 0..17 {
        name_from_text(&format!("a{i}.test"))
            .unwrap()
            .write_wire(&mut wire);
        let mut target = Vec::new();
        name_from_text(&format!("a{}.test", i + 1))
            .unwrap()
            .write_wire(&mut target);
        wire.extend(5u16.to_be_bytes());
        wire.extend(1u16.to_be_bytes());
        wire.extend(60u32.to_be_bytes());
        wire.extend(u16::try_from(target.len()).unwrap().to_be_bytes());
        wire.extend(target);
    }
    let message = parse_message(&wire).unwrap();
    assert_eq!(
        resolve_chaining(&message).unwrap_err(),
        ChainError::ChainTooLong
    );
}
