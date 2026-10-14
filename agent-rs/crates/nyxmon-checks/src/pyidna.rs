//! Python's `str.encode("idna")` (`Lib/encodings/idna.py`): IDNA 2003 with
//! nameprep (RFC 3491) and Punycode.
//!
//! Python's `ssl` and `socket` modules encode a Unicode host name with this
//! codec, so the TLS server name of a check must be encoded the same way.
//! The stringprep tables (Unicode 3.2) are generated from Python's
//! `stringprep` module into `fixtures/idna_tables.json` and embedded here.
//! NFKC follows CPython's `unicodedata.ucd_3_2_0.normalize("NFKC", ...)`
//! (`Modules/unicodedata.c`: `nfd_nfkd` + `nfc_nfkc`): compatibility
//! decomposition with the current tables (`unicode-normalization`; stable
//! for every code point assigned in Unicode 3.2) except that code points
//! unassigned in 3.2 are not decomposed and the corrigenda of `nfkc_3_2` use
//! their 3.2 mapping; canonical ordering and composition use the *current*
//! combining classes and compositions, exported from the oracle Python.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::sync::OnceLock;

use nyxmon_core::PyJson;
use nyxmon_core::pyjson::loads;

/// Python raised `UnicodeError` while encoding the host.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IdnaError(pub String);

impl fmt::Display for IdnaError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "encoding with 'idna' codec failed: {}", self.0)
    }
}

impl std::error::Error for IdnaError {}

struct Tables {
    b1: HashSet<u32>,
    b2: HashMap<u32, String>,
    prohibited: Vec<(u32, u32)>,
    d1: Vec<(u32, u32)>,
    d2: Vec<(u32, u32)>,
    unassigned_3_2: Vec<(u32, u32)>,
    nfkc_3_2: HashMap<u32, String>,
    /// `(start, end, combining class)`, sorted, non-zero classes only.
    ccc: Vec<(u32, u32, u8)>,
    compositions: HashMap<(u32, u32), char>,
    composition_firsts: HashSet<u32>,
}

fn code_of(value: &PyJson) -> u32 {
    value
        .as_i64()
        .and_then(|v| u32::try_from(v).ok())
        .expect("code point")
}

fn ranges(table: &PyJson, key: &str) -> Vec<(u32, u32)> {
    table
        .get(key)
        .and_then(PyJson::as_list)
        .expect("range table")
        .iter()
        .map(|range| {
            let pair = range.as_list().expect("range pair");
            (code_of(&pair[0]), code_of(&pair[1]))
        })
        .collect()
}

fn mapping(table: &PyJson, key: &str) -> HashMap<u32, String> {
    table
        .get(key)
        .and_then(PyJson::as_list)
        .expect("mapping table")
        .iter()
        .map(|entry| {
            let pair = entry.as_list().expect("mapping pair");
            (
                code_of(&pair[0]),
                pair[1].as_str().expect("mapped text").to_owned(),
            )
        })
        .collect()
}

fn tables() -> &'static Tables {
    static TABLES: OnceLock<Tables> = OnceLock::new();
    TABLES.get_or_init(|| {
        let table = loads(include_str!("../../../fixtures/idna_tables.json"))
            .expect("the generated IDNA tables are valid JSON");
        let compositions: Vec<(u32, u32, char)> = table
            .get("compositions")
            .and_then(PyJson::as_list)
            .expect("compositions")
            .iter()
            .map(|entry| {
                let row = entry.as_list().expect("composition row");
                let composed = char::from_u32(code_of(&row[2])).expect("scalar value");
                (code_of(&row[0]), code_of(&row[1]), composed)
            })
            .collect();
        Tables {
            b1: table
                .get("b1")
                .and_then(PyJson::as_list)
                .expect("b1")
                .iter()
                .map(code_of)
                .collect(),
            b2: mapping(&table, "b2"),
            prohibited: ranges(&table, "prohibited"),
            d1: ranges(&table, "d1"),
            d2: ranges(&table, "d2"),
            unassigned_3_2: ranges(&table, "unassigned_3_2"),
            nfkc_3_2: mapping(&table, "nfkc_3_2"),
            ccc: table
                .get("ccc")
                .and_then(PyJson::as_list)
                .expect("ccc")
                .iter()
                .map(|entry| {
                    let row = entry.as_list().expect("ccc row");
                    let class = u8::try_from(code_of(&row[2])).expect("class");
                    (code_of(&row[0]), code_of(&row[1]), class)
                })
                .collect(),
            compositions: compositions.iter().map(|&(a, b, c)| ((a, b), c)).collect(),
            composition_firsts: compositions.iter().map(|&(a, _, _)| a).collect(),
        }
    })
}

fn in_ranges(ranges: &[(u32, u32)], c: char) -> bool {
    let code = c as u32;
    let index = ranges.partition_point(|&(start, _)| start <= code);
    index > 0 && ranges[index - 1].1 >= code
}

fn combining_class(c: char) -> u8 {
    let code = c as u32;
    let ccc = &tables().ccc;
    let index = ccc.partition_point(|&(start, _, _)| start <= code);
    match index.checked_sub(1).map(|i| ccc[i]) {
        Some((_, end, class)) if end >= code => class,
        _ => 0,
    }
}

const S_BASE: u32 = 0xAC00;
const L_BASE: u32 = 0x1100;
const V_BASE: u32 = 0x1161;
const T_BASE: u32 = 0x11A7;
const L_COUNT: u32 = 19;
const V_COUNT: u32 = 21;
const T_COUNT: u32 = 28;

/// `unicodedata.ucd_3_2_0.normalize("NFKC", text)` (CPython's algorithm).
fn nfkc_3_2(text: &str) -> String {
    let tables = tables();
    // nfd_nfkd: compatibility decomposition.
    let mut chars: Vec<char> = Vec::with_capacity(text.len());
    for c in text.chars() {
        if in_ranges(&tables.unassigned_3_2, c) {
            chars.push(c);
        } else if let Some(mapped) = tables.nfkc_3_2.get(&(c as u32)) {
            chars.extend(mapped.chars());
        } else {
            unicode_normalization::char::decompose_compatible(c, |d| chars.push(d));
        }
    }
    // Canonical ordering: stable sort of every run of non-starters.
    let mut start = 0;
    while start < chars.len() {
        if combining_class(chars[start]) == 0 {
            start += 1;
            continue;
        }
        let mut end = start;
        while end < chars.len() && combining_class(chars[end]) != 0 {
            end += 1;
        }
        chars[start..end].sort_by_key(|c| combining_class(*c));
        start = end;
    }
    // nfc_nfkc: canonical composition.
    let mut out: Vec<char> = Vec::with_capacity(chars.len());
    let mut skipped = vec![false; chars.len()];
    let mut i = 0;
    while i < chars.len() {
        if skipped[i] {
            i += 1;
            continue;
        }
        let code = chars[i] as u32;
        let next = chars.get(i + 1).map(|c| *c as u32);
        if (L_BASE..L_BASE + L_COUNT).contains(&code)
            && next.is_some_and(|n| (V_BASE..V_BASE + V_COUNT).contains(&n))
        {
            let l_index = code - L_BASE;
            let v_index = next.unwrap_or(V_BASE) - V_BASE;
            let mut syllable = S_BASE + (l_index * V_COUNT + v_index) * T_COUNT;
            i += 2;
            if let Some(t) = chars.get(i).map(|c| *c as u32)
                && T_BASE < t
                && t < T_BASE + T_COUNT
            {
                syllable += t - T_BASE;
                i += 1;
            }
            out.push(char::from_u32(syllable).unwrap_or(chars[i - 1]));
            continue;
        }
        out.push(chars[i]);
        i += 1;
        if !tables.composition_firsts.contains(&code) {
            continue;
        }
        let mut comb = 0u8;
        let mut i1 = i;
        while i1 < chars.len() {
            let code1 = chars[i1];
            let comb1 = combining_class(code1);
            if comb != 0 {
                if comb1 == 0 {
                    break;
                }
                if comb >= comb1 {
                    i1 += 1;
                    continue;
                }
            }
            let last = *out.last().expect("base pushed above") as u32;
            match tables.compositions.get(&(last, code1 as u32)) {
                Some(&composed) => {
                    *out.last_mut().expect("base pushed above") = composed;
                    skipped[i1] = true;
                    i1 += 1;
                    if !tables.composition_firsts.contains(&(composed as u32)) {
                        break;
                    }
                }
                _ => {
                    if comb1 == 0 {
                        break;
                    }
                    comb = comb1;
                    i1 += 1;
                }
            }
        }
    }
    out.into_iter().collect()
}

/// `encodings.idna.nameprep`.
fn nameprep(label: &str) -> Result<String, IdnaError> {
    let tables = tables();
    let mut mapped = String::new();
    for c in label.chars() {
        if tables.b1.contains(&(c as u32)) {
            continue;
        }
        match tables.b2.get(&(c as u32)) {
            Some(replacement) => mapped.push_str(replacement),
            None => mapped.push(c),
        }
    }
    let label = nfkc_3_2(&mapped);
    if let Some(c) = label.chars().find(|c| in_ranges(&tables.prohibited, *c)) {
        return Err(IdnaError(format!("Invalid character {c:?}")));
    }
    let rand_al: Vec<bool> = label.chars().map(|c| in_ranges(&tables.d1, c)).collect();
    if rand_al.iter().any(|flag| *flag) {
        if label.chars().any(|c| in_ranges(&tables.d2, c)) {
            return Err(IdnaError("Violation of BIDI requirement 2".to_owned()));
        }
        if !rand_al[0] || !rand_al[rand_al.len() - 1] {
            return Err(IdnaError("Violation of BIDI requirement 3".to_owned()));
        }
    }
    Ok(label)
}

fn check_length(label: &str) -> Result<(), IdnaError> {
    match label.len() {
        0 => Err(IdnaError("label empty".to_owned())),
        1..=63 => Ok(()),
        _ => Err(IdnaError("label too long".to_owned())),
    }
}

/// `encodings.idna.ToASCII`.
fn to_ascii(label: &str) -> Result<String, IdnaError> {
    if label.is_ascii() {
        check_length(label)?;
        return Ok(label.to_owned());
    }
    let label = nameprep(label)?;
    if label.is_ascii() {
        check_length(&label)?;
        return Ok(label);
    }
    if label.to_lowercase().starts_with("xn--") {
        return Err(IdnaError("Label starts with ACE prefix".to_owned()));
    }
    let encoded = ::idna::punycode::encode_str(&label)
        .ok_or_else(|| IdnaError("punycode overflow".to_owned()))?;
    let ascii = format!("xn--{encoded}");
    if ascii.len() < 64 {
        Ok(ascii)
    } else {
        Err(IdnaError("label too long".to_owned()))
    }
}

/// The label separators of Python's codec.
const DOTS: [char; 4] = ['.', '\u{3002}', '\u{ff0e}', '\u{ff61}'];

/// `host.encode("idna").decode("ascii")`.
///
/// # Errors
/// Every input for which Python raises `UnicodeError`.
pub fn encode(host: &str) -> Result<String, IdnaError> {
    if host.is_empty() {
        return Ok(String::new());
    }
    if host.is_ascii() {
        // ASCII fast path: only label lengths are checked, case is kept.
        let labels: Vec<&str> = host.split('.').collect();
        if labels[..labels.len() - 1]
            .iter()
            .any(|label| label.is_empty())
        {
            return Err(IdnaError("label empty".to_owned()));
        }
        if labels.iter().any(|label| label.len() >= 64) {
            return Err(IdnaError("label too long".to_owned()));
        }
        return Ok(host.to_owned());
    }
    let mut labels: Vec<&str> = host.split(DOTS).collect();
    let trailing_dot = labels.last() == Some(&"");
    if trailing_dot {
        labels.pop();
    }
    let mut encoded = Vec::with_capacity(labels.len());
    for label in labels {
        encoded.push(to_ascii(label)?);
    }
    let mut joined = encoded.join(".");
    if trailing_dot {
        joined.push('.');
    }
    Ok(joined)
}
