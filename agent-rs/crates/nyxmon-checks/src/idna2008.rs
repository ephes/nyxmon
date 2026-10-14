//! `anyio`'s `idna2008_resolve`: the name `anyio.connect_tcp` resolves and
//! the server name `TLSStream.wrap` sends for a host (`tcp_executor.py`).
//!
//! An ASCII host passes through unchanged. Any other host goes through
//! `idna.encode(host, uts46=True)` of the `idna` package (`idna/core.py`):
//! UTS 46 mapping (non-transitional, without the STD3 ASCII rules) and NFC,
//! then per label the strict IDNA 2008 check (`check_label`: NFC, hyphens,
//! leading combining mark, the PVALID/CONTEXTJ/CONTEXTO classes with their
//! context rules, the Bidi rule) and the DNS length limits.
//!
//! The data is the oracle's, not a Rust crate's (`fixtures/idna2008_tables.json`):
//! the package's UTS 46 table, code point classes, joining types and
//! scripts, and CPython's `unicodedata` bidirectional classes, marks and
//! virama combining class. NFC uses `unicode-normalization`; its newer
//! Unicode version gives the same result for text assigned in CPython's
//! version (normalization stability), and a label with a code point CPython
//! does not know fails the Bidi rule's "unknown directionality" anyway.

use std::collections::HashMap;
use std::sync::OnceLock;

use nyxmon_core::PyJson;
use nyxmon_core::pyjson::loads;
use unicode_normalization::UnicodeNormalization;

/// Why a host cannot be IDNA-encoded (Python's `idna.IDNAError`, a
/// `UnicodeError`; its text is library-derived, PLAN.md P2).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IdnaError(pub String);

impl std::fmt::Display for IdnaError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for IdnaError {}

fn error(message: impl Into<String>) -> IdnaError {
    IdnaError(message.into())
}

type Ranges = Vec<(u32, u32)>;

struct Uts46Row {
    start: u32,
    status: char,
    replacement: Option<String>,
}

struct Tables {
    uts46: Vec<Uts46Row>,
    pvalid: Ranges,
    contextj: Ranges,
    contexto: Ranges,
    greek: Ranges,
    han: Ranges,
    hebrew: Ranges,
    hiragana: Ranges,
    katakana: Ranges,
    joining_types: HashMap<u32, char>,
    /// `(start, end, class)`, covering every code point; `""` = unknown.
    bidi: Vec<(u32, u32, String)>,
    marks: Ranges,
    virama: Ranges,
}

fn code(value: &PyJson) -> u32 {
    value
        .as_i64()
        .and_then(|v| u32::try_from(v).ok())
        .expect("a code point")
}

fn rows(value: Option<&PyJson>) -> &[PyJson] {
    value.and_then(PyJson::as_list).expect("a generated list")
}

fn ranges(value: Option<&PyJson>) -> Ranges {
    rows(value)
        .iter()
        .map(|range| {
            let pair = range.as_list().expect("a [start, end] pair");
            (code(&pair[0]), code(&pair[1]))
        })
        .collect()
}

fn tables() -> &'static Tables {
    static TABLES: OnceLock<Tables> = OnceLock::new();
    TABLES.get_or_init(|| {
        let table = loads(include_str!("../../../fixtures/idna2008_tables.json"))
            .expect("the generated IDNA table is valid JSON");
        let class = |name: &str| ranges(table.get("classes").and_then(|c| c.get(name)));
        let script = |name: &str| ranges(table.get("scripts").and_then(|s| s.get(name)));
        let text = |value: &PyJson| value.as_str().expect("text").to_owned();
        Tables {
            uts46: rows(table.get("uts46"))
                .iter()
                .map(|row| {
                    let row = row.as_list().expect("a UTS 46 row");
                    Uts46Row {
                        start: code(&row[0]),
                        status: text(&row[1]).chars().next().expect("a status"),
                        replacement: row[2].as_str().map(str::to_owned),
                    }
                })
                .collect(),
            pvalid: class("PVALID"),
            contextj: class("CONTEXTJ"),
            contexto: class("CONTEXTO"),
            greek: script("Greek"),
            han: script("Han"),
            hebrew: script("Hebrew"),
            hiragana: script("Hiragana"),
            katakana: script("Katakana"),
            joining_types: rows(table.get("joining_types"))
                .iter()
                .map(|row| {
                    let row = row.as_list().expect("a joining type");
                    (code(&row[0]), text(&row[1]).chars().next().expect("a type"))
                })
                .collect(),
            bidi: rows(table.get("bidi"))
                .iter()
                .map(|row| {
                    let row = row.as_list().expect("a bidi range");
                    (code(&row[0]), code(&row[1]), text(&row[2]))
                })
                .collect(),
            marks: ranges(table.get("marks")),
            virama: ranges(table.get("virama")),
        }
    })
}

fn contains(ranges: &[(u32, u32)], c: char) -> bool {
    let code = c as u32;
    let index = ranges.partition_point(|&(start, _)| start <= code);
    index > 0 && ranges[index - 1].1 >= code
}

/// `unicodedata.bidirectional(c)`.
fn bidi(c: char) -> &'static str {
    let table = &tables().bidi;
    let code = c as u32;
    let index = table.partition_point(|&(start, _, _)| start <= code);
    &table[index - 1].2
}

/// `uts46_remap(domain, std3_rules=False, transitional=False)`.
fn uts46_remap(domain: &str) -> Result<String, IdnaError> {
    let rows = &tables().uts46;
    let mut output = String::with_capacity(domain.len());
    for (pos, c) in domain.chars().enumerate() {
        let code = c as u32;
        let index = if code < 256 {
            code as usize
        } else {
            rows.partition_point(|row| row.start <= code) - 1
        };
        let row = &rows[index];
        let keep =
            matches!(row.status, 'V' | 'D') || (row.status == '3' && row.replacement.is_none());
        match (&row.replacement, row.status) {
            _ if keep => output.push(c),
            (Some(replacement), 'M' | '3') => output.push_str(replacement),
            (_, 'I') => {}
            _ => {
                return Err(error(format!(
                    "Codepoint U+{code:04X} not allowed at position {}",
                    pos + 1
                )));
            }
        }
    }
    Ok(output.nfc().collect())
}

/// `valid_contextj` (RFC 5892 appendix A.1, A.2).
fn valid_contextj(label: &[char], pos: usize, tables: &Tables) -> bool {
    let virama_before = pos > 0 && contains(&tables.virama, label[pos - 1]);
    match label[pos] {
        '\u{200c}' => {
            if virama_before {
                return true;
            }
            let joining = |c: char| tables.joining_types.get(&(c as u32)).copied();
            let side_ok = |chars: &mut dyn Iterator<Item = &char>, wanted: [char; 2]| {
                for &c in chars {
                    match joining(c) {
                        Some('T') => continue,
                        Some(kind) if wanted.contains(&kind) => return true,
                        _ => return false,
                    }
                }
                false
            };
            side_ok(&mut label[..pos].iter().rev(), ['L', 'D'])
                && side_ok(&mut label[pos + 1..].iter(), ['R', 'D'])
        }
        '\u{200d}' => virama_before,
        _ => false,
    }
}

/// `valid_contexto` (RFC 5892 appendix A.3-A.9).
fn valid_contexto(label: &[char], pos: usize, tables: &Tables) -> bool {
    match label[pos] as u32 {
        0x00B7 => {
            pos > 0 && pos + 1 < label.len() && label[pos - 1] == 'l' && label[pos + 1] == 'l'
        }
        0x0375 => pos + 1 < label.len() && contains(&tables.greek, label[pos + 1]),
        0x05F3 | 0x05F4 => pos > 0 && contains(&tables.hebrew, label[pos - 1]),
        0x30FB => label.iter().any(|&other| {
            other != '\u{30fb}'
                && (contains(&tables.hiragana, other)
                    || contains(&tables.katakana, other)
                    || contains(&tables.han, other))
        }),
        0x0660..=0x0669 => !label.iter().any(|&o| ('\u{6f0}'..='\u{6f9}').contains(&o)),
        0x06F0..=0x06F9 => !label.iter().any(|&o| ('\u{660}'..='\u{669}').contains(&o)),
        _ => false,
    }
}

/// `check_bidi` (RFC 5893), applied to labels holding an R, AL or AN code
/// point.
fn check_bidi(label: &[char]) -> Result<(), IdnaError> {
    let mut rtl_label = false;
    for &c in label {
        match bidi(c) {
            "" => return Err(error("Unknown directionality in label")),
            "R" | "AL" | "AN" => rtl_label = true,
            _ => {}
        }
    }
    if !rtl_label {
        return Ok(());
    }
    let rtl = match bidi(label[0]) {
        "R" | "AL" => true,
        "L" => false,
        _ => {
            return Err(error(
                "First codepoint in label must be directionality L, R or AL",
            ));
        }
    };
    let mut valid_ending = false;
    let mut number_type: Option<&str> = None;
    for &c in label {
        let direction = bidi(c);
        if rtl {
            if !matches!(
                direction,
                "R" | "AL" | "AN" | "EN" | "ES" | "CS" | "ET" | "ON" | "BN" | "NSM"
            ) {
                return Err(error("Invalid direction in a right-to-left label"));
            }
            if matches!(direction, "R" | "AL" | "EN" | "AN") {
                valid_ending = true;
            } else if direction != "NSM" {
                valid_ending = false;
            }
            if matches!(direction, "AN" | "EN") {
                match number_type {
                    None => number_type = Some(direction),
                    Some(kind) if kind != direction => {
                        return Err(error("Can not mix numeral types in a right-to-left label"));
                    }
                    Some(_) => {}
                }
            }
        } else {
            if !matches!(
                direction,
                "L" | "EN" | "ES" | "CS" | "ET" | "ON" | "BN" | "NSM"
            ) {
                return Err(error("Invalid direction in a left-to-right label"));
            }
            if matches!(direction, "L" | "EN") {
                valid_ending = true;
            } else if direction != "NSM" {
                valid_ending = false;
            }
        }
    }
    if valid_ending {
        Ok(())
    } else {
        Err(error("Invalid label ending"))
    }
}

/// `check_label`: the IDNA 2008 validity of one U-label.
fn check_label(label: &str) -> Result<(), IdnaError> {
    let tables = tables();
    let chars: Vec<char> = label.chars().collect();
    if chars.is_empty() {
        return Err(error("Empty Label"));
    }
    if chars.len() > 254 {
        return Err(error("Label too long"));
    }
    if label.nfc().ne(label.chars()) {
        return Err(error("Label must be in Normalization Form C"));
    }
    if chars.get(2..4) == Some(&['-', '-']) {
        return Err(error(
            "Label has disallowed hyphens in 3rd and 4th position",
        ));
    }
    if chars[0] == '-' || chars[chars.len() - 1] == '-' {
        return Err(error("Label must not start or end with a hyphen"));
    }
    if contains(&tables.marks, chars[0]) {
        return Err(error("Label begins with an illegal combining character"));
    }
    for (pos, &c) in chars.iter().enumerate() {
        let valid = if contains(&tables.pvalid, c) {
            true
        } else if contains(&tables.contextj, c) {
            valid_contextj(&chars, pos, tables)
        } else if contains(&tables.contexto, c) {
            valid_contexto(&chars, pos, tables)
        } else {
            false
        };
        if !valid {
            return Err(error(format!(
                "Codepoint U+{:04X} at position {} not allowed",
                c as u32,
                pos + 1
            )));
        }
    }
    check_bidi(&chars)
}

/// `alabel`: the A-label of one label.
fn alabel(label: &str) -> Result<String, IdnaError> {
    if label.is_ascii() {
        // `ulabel(label_bytes)` validates the ASCII label.
        let lower = label.to_ascii_lowercase();
        match lower.strip_prefix("xn--") {
            Some(encoded) => {
                if encoded.is_empty() {
                    return Err(error(
                        "Malformed A-label, no Punycode eligible content found",
                    ));
                }
                if encoded.ends_with('-') {
                    return Err(error("A-label must not end with a hyphen"));
                }
                let decoded = idna::punycode::decode_to_string(encoded)
                    .ok_or_else(|| error("Invalid A-label"))?;
                check_label(&decoded)?;
            }
            None => check_label(&lower)?,
        }
        if label.len() > 63 {
            return Err(error("Label too long"));
        }
        return Ok(label.to_owned());
    }
    check_label(label)?;
    let encoded = idna::punycode::encode_str(label).ok_or_else(|| error("Invalid label"))?;
    let alabel = format!("xn--{encoded}");
    if alabel.len() > 63 {
        return Err(error("Label too long"));
    }
    Ok(alabel)
}

/// `idna2008_resolve(host)` as text: the host itself when it is ASCII,
/// otherwise `idna.encode(host, uts46=True)`.
///
/// # Errors
/// The host is not a valid IDNA 2008 domain name.
pub fn idna2008_resolve(host: &str) -> Result<String, IdnaError> {
    if host.is_ascii() {
        return Ok(host.to_owned());
    }
    let mapped = uts46_remap(host)?;
    if mapped.chars().count() > 254 {
        return Err(error("Domain too long"));
    }
    let mut labels: Vec<&str> = mapped
        .split(['.', '\u{3002}', '\u{ff0e}', '\u{ff61}'])
        .collect();
    if labels == [""] {
        return Err(error("Empty domain"));
    }
    let trailing_dot = labels.last() == Some(&"");
    if trailing_dot {
        labels.pop();
    }
    let mut result = labels
        .into_iter()
        .map(alabel)
        .collect::<Result<Vec<String>, IdnaError>>()?;
    if trailing_dot {
        result.push(String::new());
    }
    let encoded = result.join(".");
    if encoded.len() > if trailing_dot { 254 } else { 253 } {
        return Err(error("Domain too long"));
    }
    Ok(encoded)
}

#[cfg(test)]
mod tests {
    use super::*;
    use nyxmon_core::pyjson::loads;

    #[test]
    fn matches_the_idna_package() {
        let fixture = loads(include_str!("../../../fixtures/config_tcp.json")).unwrap();
        let cases = fixture.get("idna2008").and_then(PyJson::as_list).unwrap();
        assert!(cases.len() > 450);
        for case in cases {
            let host = case.get("host").and_then(PyJson::as_str).unwrap();
            let expected = case.get("ascii").and_then(PyJson::as_str);
            assert_eq!(idna2008_resolve(host).ok().as_deref(), expected, "{host:?}");
        }
    }
}
