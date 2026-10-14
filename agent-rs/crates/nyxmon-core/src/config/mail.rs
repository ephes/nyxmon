//! Helpers shared by the mail check configurations (SMTP, IMAP):
//! Python line counting, `data` lookups on non-dict values (PLAN.md D-11),
//! exact `int` checks and the duration rule.

use crate::pyjson::{PyJson, py_type_name};

/// `len(text.splitlines())`: the number of lines Python's `str.splitlines`
/// produces (line boundaries `\n`, `\r`, `\r\n`, `\v`, `\f`, `\x1c`-`\x1e`,
/// `\x85`, U+2028, U+2029).
pub fn py_splitlines_count(text: &str) -> usize {
    let mut count = 0;
    let mut chars = text.chars().peekable();
    let mut in_line = false;
    while let Some(c) = chars.next() {
        let boundary = matches!(
            c,
            '\n' | '\r'
                | '\u{b}'
                | '\u{c}'
                | '\u{1c}'
                | '\u{1d}'
                | '\u{1e}'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        );
        if boundary {
            if c == '\r' && chars.peek() == Some(&'\n') {
                chars.next();
            }
            count += 1;
            in_line = false;
        } else {
            in_line = true;
        }
    }
    count + usize::from(in_line)
}

/// How `key in data` and `data[key]` behave for a check's `data` value that
/// is not a dict (PLAN.md D-11): the configuration error message, or `None`
/// when `data` is a dict.
///
/// `in` tests membership for lists and substrings for strings, so a list or
/// string *containing* the key fails only at the subscript.
pub fn non_dict_lookup_error(data: &PyJson, key: &str, missing: &str) -> Option<String> {
    let contains = match data {
        PyJson::Dict(_) | PyJson::Null => return None,
        PyJson::List(items) => items.iter().any(|item| item.as_str() == Some(key)),
        PyJson::Str(text) => text.contains(key),
        other => {
            return Some(format!(
                "argument of type '{}' is not iterable",
                py_type_name(other)
            ));
        }
    };
    Some(match (data, contains) {
        (_, false) => missing.to_owned(),
        (PyJson::List(_), true) => "list indices must be integers or slices, not str".to_owned(),
        _ => "string indices must be integers, not 'str'".to_owned(),
    })
}

/// `type(value) is int` (not `bool`), as an `i64` when it fits.
pub fn exact_int(value: &PyJson) -> Option<IntValue> {
    match value {
        PyJson::Int(n) => Some(IntValue::Small(*n)),
        PyJson::BigInt(n) => Some(IntValue::Big(n.sign() == num_bigint::Sign::Minus)),
        _ => None,
    }
}

/// An exact Python `int`: small, or beyond `i64` with its sign (`true` when
/// negative).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum IntValue {
    Small(i64),
    Big(bool),
}

impl IntValue {
    /// `low <= value <= high`.
    pub fn within(self, low: i64, high: i64) -> bool {
        matches!(self, IntValue::Small(n) if (low..=high).contains(&n))
    }
}

/// The duration check shared by the mail configurations:
/// `type(v) in (int, float)`, `0 <= v <= 300`, finite, and for `timeout`
/// not zero. Returns the value in seconds or the Python message.
pub fn mail_duration(name: &str, value: &PyJson) -> Result<f64, String> {
    let is_timeout = name == "timeout";
    let seconds = match value {
        PyJson::Int(n) if (0..=300).contains(n) => Some(*n as f64),
        PyJson::Float(x) if x.is_finite() && (0.0..=300.0).contains(x) => Some(*x),
        _ => None,
    };
    match seconds {
        Some(s) if !(is_timeout && s == 0.0) => Ok(s),
        _ => Err(format!(
            "{name} must be a finite number {}between 0 and 300 seconds",
            if is_timeout {
                "greater than zero and "
            } else {
                ""
            }
        )),
    }
}

/// `dict.get(key, default)` on a check's data dict.
pub fn get<'a>(data: &'a PyJson, key: &str) -> Option<&'a PyJson> {
    data.as_dict().and_then(|map| map.get(key))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pyjson::loads;

    fn json(text: &str) -> PyJson {
        loads(text).expect("valid JSON")
    }

    #[test]
    fn splitlines_counts_python_line_boundaries() {
        assert_eq!(py_splitlines_count(""), 0);
        assert_eq!(py_splitlines_count("a"), 1);
        assert_eq!(py_splitlines_count("a\r\n"), 1);
        assert_eq!(py_splitlines_count("a\r\nb"), 2);
        assert_eq!(py_splitlines_count("a\u{b}b"), 2);
        assert_eq!(py_splitlines_count("a\u{85}"), 1);
        assert_eq!(py_splitlines_count("\n\n"), 2);
        assert_eq!(py_splitlines_count("a\u{2029}b\u{1c}c"), 3);
    }

    #[test]
    fn non_dict_data_fails_like_python_membership() {
        let missing = "host is required";
        assert_eq!(non_dict_lookup_error(&json("{}"), "host", missing), None);
        assert_eq!(
            non_dict_lookup_error(&json("5"), "host", missing).as_deref(),
            Some("argument of type 'int' is not iterable")
        );
        assert_eq!(
            non_dict_lookup_error(&json(r#"["x"]"#), "host", missing).as_deref(),
            Some(missing)
        );
        assert_eq!(
            non_dict_lookup_error(&json(r#""a host""#), "host", missing).as_deref(),
            Some("string indices must be integers, not 'str'")
        );
    }
}
