//! `str` methods whose result depends on the Unicode database: they follow
//! the oracle CPython's database (`fixtures/unicode_props.json`, Unicode
//! 15.1 for CPython 3.13) instead of the Rust standard library's version.

use std::collections::HashMap;
use std::sync::OnceLock;

use super::repr::{in_ranges, ranges_of};
use super::{PyJson, loads};

struct Props {
    digits: Vec<(u32, u32)>,
    lower: HashMap<char, String>,
    cased: Vec<(u32, u32)>,
    case_ignorable: Vec<(u32, u32)>,
    nfkc_url_delimiters: Vec<char>,
}

fn props() -> &'static Props {
    static PROPS: OnceLock<Props> = OnceLock::new();
    PROPS.get_or_init(|| {
        let table = loads(include_str!("../../../../fixtures/unicode_props.json"))
            .expect("the generated Unicode table is valid JSON");
        let code = |value: &PyJson| {
            value
                .as_i64()
                .and_then(|v| u32::try_from(v).ok())
                .and_then(char::from_u32)
                .expect("a code point")
        };
        let list = |key: &str| {
            table
                .get(key)
                .and_then(PyJson::as_list)
                .expect("a generated list")
        };
        Props {
            digits: ranges_of(table.get("digits")),
            lower: list("lower")
                .iter()
                .map(|row| {
                    let row = row.as_list().expect("a [code, lower] pair");
                    (code(&row[0]), row[1].as_str().expect("text").to_owned())
                })
                .collect(),
            cased: ranges_of(table.get("cased")),
            case_ignorable: ranges_of(table.get("case_ignorable")),
            nfkc_url_delimiters: list("nfkc_url_delimiters").iter().map(code).collect(),
        }
    })
}

/// `str.isdigit()` for one character (`Numeric_Type` Digit or Decimal).
pub fn py_isdigit(c: char) -> bool {
    in_ranges(&props().digits, c)
}

/// CPython's final-sigma rule (`Objects/unicodeobject.c:handle_capital_sigma`)
/// for the capital sigma at `index`.
fn final_sigma(chars: &[char], index: usize) -> bool {
    let props = props();
    let ignorable = |c: char| in_ranges(&props.case_ignorable, c);
    let cased = |c: char| in_ranges(&props.cased, c);
    let before = chars[..index].iter().rev().find(|&&c| !ignorable(c));
    if !before.is_some_and(|&c| cased(c)) {
        return false;
    }
    let after = chars[index + 1..].iter().find(|&&c| !ignorable(c));
    after.is_none_or(|&c| !cased(c))
}

/// `str.lower()`: full lowercase mapping with the final-sigma rule.
pub fn py_lower(text: &str) -> String {
    let props = props();
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    for (index, &c) in chars.iter().enumerate() {
        if c == '\u{3a3}' {
            out.push(if final_sigma(&chars, index) {
                '\u{3c2}'
            } else {
                '\u{3c3}'
            });
        } else if let Some(lower) = props.lower.get(&c) {
            out.push_str(lower);
        } else {
            out.push(c);
        }
    }
    out
}

/// Whether the NFKC form of `c` contains one of `/?#@:` while `c` is not one
/// of them (`urllib.parse._checknetloc`).
pub fn nfkc_adds_url_delimiter(c: char) -> bool {
    props().nfkc_url_delimiters.contains(&c)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lowercase_follows_the_oracle() {
        assert_eq!(py_lower("ABC\u{130}"), "abci\u{307}");
        // Unassigned in Unicode 15.1, so CPython 3.13 keeps it.
        assert_eq!(py_lower("\u{a7cb}"), "\u{a7cb}");
        assert_eq!(py_lower("\u{3a3}"), "\u{3c3}");
        assert_eq!(py_lower("A\u{3a3}"), "a\u{3c2}");
        assert_eq!(py_lower("A\u{3a3}B"), "a\u{3c3}b");
        assert_eq!(py_lower("A\u{3a3}'"), "a\u{3c2}'");
        assert_eq!(py_lower("A'\u{3a3}"), "a'\u{3c2}");
    }

    #[test]
    fn digits_and_delimiters() {
        assert!(py_isdigit('7') && py_isdigit('\u{b2}') && py_isdigit('\u{662}'));
        assert!(!py_isdigit('a') && !py_isdigit('\u{bd}'));
        assert!(nfkc_adds_url_delimiter('\u{2100}'));
        assert!(nfkc_adds_url_delimiter('\u{ff0f}'));
        assert!(!nfkc_adds_url_delimiter('/') && !nfkc_adds_url_delimiter('a'));
    }
}
