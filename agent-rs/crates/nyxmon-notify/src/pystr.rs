//! Python `str()` and `repr()` of JSON values, as the notifier formats
//! result data (`str(error_msg)`) and response bodies (`body=%s`).

use nyxmon_core::pyjson::{PyJson, py_float_repr, py_str_repr};

enum Task<'a> {
    Value(&'a PyJson),
    Text(&'static str),
    Key(&'a str),
}

/// `repr(value)` of a decoded JSON value (`None`, `True`, `'text'`,
/// `[1, 2.5]`, `{'k': 'v'}`).
///
/// Iterative, so deeply nested endpoint data cannot overflow the stack.
pub fn py_repr(value: &PyJson) -> String {
    let mut out = String::new();
    let mut tasks = vec![Task::Value(value)];
    while let Some(task) = tasks.pop() {
        match task {
            Task::Text(text) => out.push_str(text),
            Task::Key(key) => {
                out.push_str(&py_str_repr(key));
                out.push_str(": ");
            }
            Task::Value(value) => match value {
                PyJson::Null => out.push_str("None"),
                PyJson::Bool(true) => out.push_str("True"),
                PyJson::Bool(false) => out.push_str("False"),
                PyJson::Int(number) => out.push_str(&number.to_string()),
                PyJson::BigInt(number) => out.push_str(&number.to_string()),
                PyJson::Float(number) => out.push_str(&py_float_repr(*number)),
                PyJson::Str(text) => out.push_str(&py_str_repr(text)),
                PyJson::List(items) => {
                    out.push('[');
                    tasks.push(Task::Text("]"));
                    for (index, item) in items.iter().enumerate().rev() {
                        tasks.push(Task::Value(item));
                        if index > 0 {
                            tasks.push(Task::Text(", "));
                        }
                    }
                }
                PyJson::Dict(map) => {
                    out.push('{');
                    tasks.push(Task::Text("}"));
                    for (index, (key, item)) in map.iter().enumerate().rev() {
                        tasks.push(Task::Value(item));
                        tasks.push(Task::Key(key));
                        if index > 0 {
                            tasks.push(Task::Text(", "));
                        }
                    }
                }
            },
        }
    }
    out
}

/// `str(value)`: the text itself for a string, `repr` otherwise.
pub fn py_str(value: &PyJson) -> String {
    match value {
        PyJson::Str(text) => text.clone(),
        other => py_repr(other),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use nyxmon_core::pyjson::loads;

    #[test]
    fn repr_matches_python() {
        let cases = [
            ("null", "None"),
            (
                "[true, false, 1, -2.5, 1e16, NaN, -Infinity]",
                "[True, False, 1, -2.5, 1e+16, nan, -inf]",
            ),
            ("{}", "{}"),
            ("[]", "[]"),
            (
                r#"{"a": {"b": [1, {"c": "it's"}]}, "d": "q\"'"}"#,
                r#"{'a': {'b': [1, {'c': "it's"}]}, 'd': 'q"\''}"#,
            ),
            (
                "123456789012345678901234567890",
                "123456789012345678901234567890",
            ),
        ];
        for (json, expected) in cases {
            assert_eq!(py_repr(&loads(json).unwrap()), expected, "{json}");
        }
        assert_eq!(py_str(&PyJson::from("plain 'x'")), "plain 'x'");
    }

    #[test]
    fn repr_survives_deep_nesting() {
        let depth = 100_000;
        let json = format!("{}{}", "[".repeat(depth), "]".repeat(depth));
        let mut value = PyJson::List(Vec::new());
        for _ in 1..depth {
            value = PyJson::List(vec![value]);
        }
        assert_eq!(py_repr(&value), json);
    }
}
