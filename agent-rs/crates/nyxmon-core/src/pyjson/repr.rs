//! Python string helpers: `str.isspace`, `str.strip()`, `len`, `repr(str)`,
//! `repr(bytes)`, and `repr()`/`str()` of JSON values.

use std::fmt::Write as _;
use std::sync::OnceLock;

use super::num::py_float_repr;
use super::{PyJson, loads};

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

/// Parse a generated `[[start, end], ...]` code point range list.
pub(super) fn ranges_of(value: Option<&PyJson>) -> Vec<(u32, u32)> {
    value
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
}

pub(super) fn in_ranges(ranges: &[(u32, u32)], c: char) -> bool {
    let code = c as u32;
    let index = ranges.partition_point(|&(start, _)| start <= code);
    index > 0 && ranges[index - 1].1 >= code
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
        ranges_of(table.get("ranges"))
    })
}

/// `str.isprintable()` for one character (`Py_UNICODE_ISPRINTABLE`).
fn is_printable(c: char) -> bool {
    !in_ranges(nonprintable_ranges(), c)
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

/// `repr(value)` for a decoded JSON value (`None`, `True`, `'text'`,
/// `[1, 2.0]`, `{'key': None}`), as `%r` renders it in a log message.
///
/// Iterative, so hostile nesting cannot overflow the stack (CPython would
/// raise `RecursionError` beyond its recursion limit instead).
pub fn py_repr(value: &PyJson) -> String {
    enum Item<'a> {
        Value(&'a PyJson),
        Key(&'a str),
        Text(&'static str),
    }
    let mut out = String::new();
    let mut pending = vec![Item::Value(value)];
    while let Some(item) = pending.pop() {
        match item {
            Item::Text(text) => out.push_str(text),
            Item::Key(key) => {
                out.push_str(&py_str_repr(key));
                out.push_str(": ");
            }
            Item::Value(value) => match value {
                PyJson::Null => out.push_str("None"),
                PyJson::Bool(true) => out.push_str("True"),
                PyJson::Bool(false) => out.push_str("False"),
                PyJson::Int(number) => {
                    let _ = write!(out, "{number}");
                }
                PyJson::BigInt(number) => {
                    let _ = write!(out, "{number}");
                }
                PyJson::Float(number) => out.push_str(&py_float_repr(*number)),
                PyJson::Str(text) => out.push_str(&py_str_repr(text)),
                PyJson::List(items) => {
                    out.push('[');
                    pending.push(Item::Text("]"));
                    for (index, item) in items.iter().enumerate().rev() {
                        pending.push(Item::Value(item));
                        if index > 0 {
                            pending.push(Item::Text(", "));
                        }
                    }
                }
                PyJson::Dict(map) => {
                    out.push('{');
                    pending.push(Item::Text("}"));
                    for (index, (key, item)) in map.iter().enumerate().rev() {
                        pending.push(Item::Value(item));
                        pending.push(Item::Key(key));
                        if index > 0 {
                            pending.push(Item::Text(", "));
                        }
                    }
                }
            },
        }
    }
    out
}

/// `str(value)` for a decoded JSON value: the text of a string, otherwise
/// [`py_repr`].
pub fn py_str(value: &PyJson) -> String {
    match value {
        PyJson::Str(text) => text.clone(),
        other => py_repr(other),
    }
}
