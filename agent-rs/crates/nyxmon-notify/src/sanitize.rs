//! Bounding of monitored-endpoint data before it reaches an OpsGate ticket
//! (`notification.py:_sanitize_untrusted_text`, `sanitize_untrusted_data`).

use std::sync::OnceLock;

use nyxmon_core::pyjson::{PyDict, PyJson, char_len, loads};

/// `UNTRUSTED_VALUE_MAX_CHARS`: longest string kept from endpoint data.
pub const UNTRUSTED_VALUE_MAX_CHARS: usize = 200;
/// `UNTRUSTED_MAX_ITEMS`: most entries kept from one dict or list.
pub const UNTRUSTED_MAX_ITEMS: usize = 25;
/// `UNTRUSTED_MAX_DEPTH`: deepest nesting kept.
pub const UNTRUSTED_MAX_DEPTH: usize = 4;

/// Code point ranges of the Unicode categories Cc, Cf, Zl and Zp in the
/// oracle Python's Unicode database (`fixtures/unicode_untrusted.json`), so
/// the replacement follows Python rather than a Rust crate's Unicode version.
fn replaced_ranges() -> &'static [(u32, u32)] {
    static RANGES: OnceLock<Vec<(u32, u32)>> = OnceLock::new();
    RANGES.get_or_init(|| {
        let table = loads(include_str!("../../../fixtures/unicode_untrusted.json"))
            .expect("the generated Unicode table is valid JSON");
        table
            .get("ranges")
            .and_then(PyJson::as_list)
            .expect("the Unicode table has ranges")
            .iter()
            .map(|range| {
                let bound = |index: usize| {
                    range
                        .as_list()
                        .and_then(|r| r.get(index))
                        .and_then(PyJson::as_i64)
                        .and_then(|v| u32::try_from(v).ok())
                        .expect("code point range bounds are integers")
                };
                (bound(0), bound(1))
            })
            .collect()
    })
}

/// `unicodedata.category(c) in {"Cc", "Cf", "Zl", "Zp"}`.
fn is_replaced(c: char) -> bool {
    let code = c as u32;
    let ranges = replaced_ranges();
    let index = ranges.partition_point(|&(start, _)| start <= code);
    index > 0 && ranges[index - 1].1 >= code
}

/// `value[:max_chars] + "...[truncated N chars]"` when `value` is longer than
/// `max_chars` code points (`notification.py:_truncate`).
pub fn truncate_chars(value: &str, max_chars: usize) -> String {
    let len = char_len(value);
    if len <= max_chars {
        return value.to_owned();
    }
    let kept: String = value.chars().take(max_chars).collect();
    format!("{kept}...[truncated {} chars]", len - max_chars)
}

/// `_sanitize_untrusted_text`: control and format characters become `?`,
/// then the text is cut to `max_chars` (`None` keeps the full length).
pub fn sanitize_untrusted_text(value: &str, max_chars: Option<usize>) -> String {
    let cleaned: String = value
        .chars()
        .map(|c| if is_replaced(c) { '?' } else { c })
        .collect();
    match max_chars {
        Some(max) => truncate_chars(&cleaned, max),
        None => cleaned,
    }
}

/// `sanitize_untrusted_data(value)`: strings and keys cut and cleaned,
/// containers bounded to [`UNTRUSTED_MAX_ITEMS`] entries and
/// [`UNTRUSTED_MAX_DEPTH`] levels; numbers, booleans and `None` unchanged.
pub fn sanitize_untrusted_data(value: &PyJson) -> PyJson {
    sanitize(value, 0)
}

/// `sanitize_untrusted_data` of a dict (the result data).
pub fn sanitize_untrusted_dict(map: &PyDict) -> PyJson {
    sanitize_dict(map, 0)
}

fn sanitize(value: &PyJson, depth: usize) -> PyJson {
    match value {
        PyJson::Null | PyJson::Bool(_) | PyJson::Int(_) | PyJson::BigInt(_) | PyJson::Float(_) => {
            value.clone()
        }
        PyJson::Str(text) => PyJson::Str(sanitize_untrusted_text(
            text,
            Some(UNTRUSTED_VALUE_MAX_CHARS),
        )),
        PyJson::List(_) | PyJson::Dict(_) if depth >= UNTRUSTED_MAX_DEPTH => {
            PyJson::from("[nested data omitted]")
        }
        PyJson::Dict(map) => sanitize_dict(map, depth),
        PyJson::List(items) => {
            let mut kept: Vec<PyJson> = items
                .iter()
                .take(UNTRUSTED_MAX_ITEMS)
                .map(|item| sanitize(item, depth + 1))
                .collect();
            if items.len() > UNTRUSTED_MAX_ITEMS {
                kept.push(PyJson::Str(format!(
                    "[{} more items omitted]",
                    items.len() - UNTRUSTED_MAX_ITEMS
                )));
            }
            PyJson::List(kept)
        }
    }
}

fn sanitize_dict(map: &PyDict, depth: usize) -> PyJson {
    if depth >= UNTRUSTED_MAX_DEPTH {
        return PyJson::from("[nested data omitted]");
    }
    let mut sanitized = PyDict::new();
    for (key, item) in map.iter().take(UNTRUSTED_MAX_ITEMS) {
        // Like a Python dict assignment: a key that sanitizes to an earlier
        // key replaces that value in place.
        sanitized.insert(
            sanitize_untrusted_text(key, Some(UNTRUSTED_VALUE_MAX_CHARS)),
            sanitize(item, depth + 1),
        );
    }
    if map.len() > UNTRUSTED_MAX_ITEMS {
        sanitized.insert(
            "[omitted]".to_owned(),
            PyJson::Str(format!("{} more entries", map.len() - UNTRUSTED_MAX_ITEMS)),
        );
    }
    PyJson::Dict(sanitized)
}
