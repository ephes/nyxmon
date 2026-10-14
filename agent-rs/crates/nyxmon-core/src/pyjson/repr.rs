//! Python string helpers: `str.isspace`, `str.strip()`, `len`, `repr(str)`
//! and `repr(bytes)`.

use std::fmt::Write as _;
use std::sync::OnceLock;

use super::loads;

/// `str.isspace()` for one character: Unicode `White_Space` plus the ASCII
/// separators U+001C..U+001F, which Python also treats as whitespace.
pub fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// `str.strip()` without arguments.
pub fn py_strip(text: &str) -> &str {
    text.trim_matches(py_isspace)
}

/// `len(text)`: the number of code points.
pub fn char_len(text: &str) -> usize {
    text.chars().count()
}

/// Code point ranges that are not printable for the oracle CPython, generated
/// by `fixtures/generate.py` from `str.isprintable()`, so `repr` follows
/// CPython's Unicode database version rather than a Rust crate's.
fn nonprintable_ranges() -> &'static [(u32, u32)] {
    static RANGES: OnceLock<Vec<(u32, u32)>> = OnceLock::new();
    RANGES.get_or_init(|| {
        let table = loads(include_str!(
            "../../../../fixtures/unicode_nonprintable.json"
        ))
        .expect("the generated Unicode table is valid JSON");
        table
            .get("ranges")
            .and_then(|ranges| ranges.as_list())
            .expect("the Unicode table has ranges")
            .iter()
            .map(|range| {
                let bound = |index: usize| {
                    range
                        .as_list()
                        .and_then(|r| r.get(index))
                        .and_then(|v| v.as_i64())
                };
                let (start, end) = (bound(0), bound(1));
                let as_code = |v: Option<i64>| {
                    v.and_then(|v| u32::try_from(v).ok())
                        .expect("code point range bounds are integers")
                };
                (as_code(start), as_code(end))
            })
            .collect()
    })
}

/// `str.isprintable()` for one character (`Py_UNICODE_ISPRINTABLE`).
fn is_printable(c: char) -> bool {
    let code = c as u32;
    let ranges = nonprintable_ranges();
    let index = ranges.partition_point(|&(start, _)| start <= code);
    index == 0 || ranges[index - 1].1 < code
}

/// `repr(text)` for a `str`.
pub fn py_str_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x7f && (c as u32) >= 0x20 => out.push(c),
            c if (c as u32) < 0x80 || !is_printable(c) => {
                let code = c as u32;
                let _ = if code <= 0xff {
                    write!(out, "\\x{code:02x}")
                } else if code <= 0xffff {
                    write!(out, "\\u{code:04x}")
                } else {
                    write!(out, "\\U{code:08x}")
                };
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// `repr(data)` for `bytes`.
pub fn py_bytes_repr(data: &[u8]) -> String {
    let quote = if data.contains(&b'\'') && !data.contains(&b'"') {
        b'"'
    } else {
        b'\''
    };
    let mut out = String::with_capacity(data.len() + 3);
    out.push('b');
    out.push(char::from(quote));
    for &byte in data {
        match byte {
            b'\\' => out.push_str("\\\\"),
            b'\t' => out.push_str("\\t"),
            b'\n' => out.push_str("\\n"),
            b'\r' => out.push_str("\\r"),
            b if b == quote => {
                out.push('\\');
                out.push(char::from(b));
            }
            0x20..=0x7e => out.push(char::from(byte)),
            _ => {
                let _ = write!(out, "\\x{byte:02x}");
            }
        }
    }
    out.push(char::from(quote));
    out
}
