//! `json.dumps` equivalent (CPython `Lib/json/encoder.py` and the C encoder).

use std::fmt;

use super::num::py_float_repr;
use super::{PyDict, PyJson};

/// Options of `json.dumps` that the agent uses.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DumpOpts {
    pub ensure_ascii: bool,
    pub sort_keys: bool,
    /// `indent=n`: newline plus `n` spaces per level.
    pub indent: Option<usize>,
    pub item_separator: &'static str,
    pub key_separator: &'static str,
    pub allow_nan: bool,
}

impl DumpOpts {
    /// `json.dumps(value)`: the storage format of `check_result.data` and
    /// `collector_incident.payload` (`sqlite_repo.py`).
    pub fn python_default() -> Self {
        DumpOpts {
            ensure_ascii: true,
            sort_keys: false,
            indent: None,
            item_separator: ", ",
            key_separator: ": ",
            allow_nan: true,
        }
    }

    /// `json.dumps(actual, default=str, ensure_ascii=False)`
    /// (`json_metrics_executor.py:_bounded_actual`).
    pub fn bounded_actual() -> Self {
        DumpOpts {
            ensure_ascii: false,
            ..DumpOpts::python_default()
        }
    }

    /// `json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True)`
    /// (`notification.py:_build_opsgate_prompt`). With an indent Python's
    /// default item separator is `","`.
    pub fn opsgate_block() -> Self {
        DumpOpts {
            ensure_ascii: true,
            sort_keys: true,
            indent: Some(2),
            item_separator: ",",
            key_separator: ": ",
            allow_nan: true,
        }
    }

    /// httpx 0.28 `json=` request bodies:
    /// `json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)`.
    pub fn httpx_json() -> Self {
        DumpOpts {
            ensure_ascii: false,
            sort_keys: false,
            indent: None,
            item_separator: ",",
            key_separator: ":",
            allow_nan: false,
        }
    }
}

/// `ValueError` raised by `json.dumps` with `allow_nan=False`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DumpError(pub String);

impl fmt::Display for DumpError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for DumpError {}

impl From<fmt::Error> for DumpError {
    fn from(_: fmt::Error) -> Self {
        Self("JSON output writer stopped".to_owned())
    }
}

/// `json.dumps(value, **opts)`.
///
/// Iterative, so arbitrarily nested values cannot exhaust the thread stack.
///
/// # Errors
/// [`DumpError`] for a non-finite float when `allow_nan` is false.
pub fn dumps(value: &PyJson, opts: &DumpOpts) -> Result<String, DumpError> {
    let mut out = String::new();
    dump_to(value, opts, &mut out)?;
    Ok(out)
}

/// Render at most `limit` code points; stop traversal as soon as the writer fills.
/// Small values retain the exact Python-compatible representation.
pub(crate) fn actual_prefix(value: &PyJson, limit: usize) -> (String, bool) {
    let mut writer = PrefixWriter {
        text: String::new(),
        remaining: limit,
        truncated: false,
    };
    let result = dump_to(value, &DumpOpts::bounded_actual(), &mut writer);
    debug_assert!(result.is_ok() || writer.truncated);
    (writer.text, writer.truncated)
}

struct PrefixWriter {
    text: String,
    remaining: usize,
    truncated: bool,
}

impl fmt::Write for PrefixWriter {
    fn write_str(&mut self, text: &str) -> fmt::Result {
        for c in text.chars() {
            if self.remaining == 0 {
                self.truncated = true;
                return Err(fmt::Error);
            }
            self.text.push(c);
            self.remaining -= 1;
        }
        Ok(())
    }
}

fn dump_to(value: &PyJson, opts: &DumpOpts, out: &mut impl fmt::Write) -> Result<(), DumpError> {
    let mut tasks = vec![Task::Value(value, 0)];
    while let Some(task) = tasks.pop() {
        match task {
            Task::Separator(level) => {
                out.write_str(opts.item_separator)?;
                newline_indent(opts, level, out)?;
            }
            Task::Key(key) => {
                try_encode_str(key, opts.ensure_ascii, out)?;
                out.write_str(opts.key_separator)?;
            }
            Task::Close(bracket, level) => {
                newline_indent(opts, level, out)?;
                out.write_char(bracket)?;
            }
            Task::List(items, index, level) => {
                if let Some(item) = items.get(index) {
                    tasks.push(Task::List(items, index + 1, level));
                    tasks.push(Task::Value(item, level));
                    if index > 0 {
                        tasks.push(Task::Separator(level));
                    }
                }
            }
            Task::Dict(map, index, level) => {
                if let Some((key, item)) = map.get_index(index) {
                    tasks.push(Task::Dict(map, index + 1, level));
                    tasks.push(Task::Value(item, level));
                    tasks.push(Task::Key(key));
                    if index > 0 {
                        tasks.push(Task::Separator(level));
                    }
                }
            }
            Task::Value(value, level) => match value {
                PyJson::Null => out.write_str("null")?,
                PyJson::Bool(true) => out.write_str("true")?,
                PyJson::Bool(false) => out.write_str("false")?,
                PyJson::Int(number) => {
                    write!(out, "{number}")?;
                }
                PyJson::BigInt(number) => {
                    write!(out, "{number}")?;
                }
                PyJson::Float(number) => encode_float(*number, opts, out)?,
                PyJson::Str(text) => try_encode_str(text, opts.ensure_ascii, out)?,
                PyJson::List(items) if items.is_empty() => out.write_str("[]")?,
                PyJson::Dict(map) if map.is_empty() => out.write_str("{}")?,
                PyJson::List(items) => {
                    out.write_char('[')?;
                    newline_indent(opts, level + 1, out)?;
                    tasks.push(Task::Close(']', level));
                    tasks.push(Task::List(items, 0, level + 1));
                }
                PyJson::Dict(map) if !opts.sort_keys => {
                    out.write_char('{')?;
                    newline_indent(opts, level + 1, out)?;
                    tasks.push(Task::Close('}', level));
                    tasks.push(Task::Dict(map, 0, level + 1));
                }
                PyJson::Dict(map) => {
                    let mut entries: Vec<(&String, &PyJson)> = map.iter().collect();
                    if opts.sort_keys {
                        entries.sort_by(|a, b| a.0.cmp(b.0));
                    }
                    out.write_char('{')?;
                    newline_indent(opts, level + 1, out)?;
                    tasks.push(Task::Close('}', level));
                    for (index, (key, item)) in entries.into_iter().enumerate().rev() {
                        tasks.push(Task::Value(item, level + 1));
                        tasks.push(Task::Key(key));
                        if index > 0 {
                            tasks.push(Task::Separator(level + 1));
                        }
                    }
                }
            },
        }
    }
    Ok(())
}

/// Pending output of [`dumps`], popped from the end.
enum Task<'a> {
    Value(&'a PyJson, usize),
    List(&'a [PyJson], usize, usize),
    Dict(&'a PyDict, usize, usize),
    Key(&'a str),
    /// Item separator, then the newline/indent of the given level.
    Separator(usize),
    /// Newline/indent of the given level, then the closing bracket.
    Close(char, usize),
}

fn newline_indent(opts: &DumpOpts, level: usize, out: &mut impl fmt::Write) -> fmt::Result {
    if let Some(width) = opts.indent {
        out.write_char('\n')?;
        for _ in 0..width * level {
            out.write_char(' ')?;
        }
    }
    Ok(())
}

fn encode_float(number: f64, opts: &DumpOpts, out: &mut impl fmt::Write) -> Result<(), DumpError> {
    if number.is_finite() {
        out.write_str(&py_float_repr(number))?;
        return Ok(());
    }
    if !opts.allow_nan {
        return Err(DumpError(format!(
            "Out of range float values are not JSON compliant: {}",
            py_float_repr(number)
        )));
    }
    out.write_str(if number.is_nan() {
        "NaN"
    } else if number > 0.0 {
        "Infinity"
    } else {
        "-Infinity"
    })?;
    Ok(())
}

fn push_unicode_escape(unit: u32, out: &mut impl fmt::Write) -> fmt::Result {
    write!(out, "\\u{unit:04x}")
}

/// `py_encode_basestring_ascii` / `py_encode_basestring`.
fn try_encode_str(text: &str, ensure_ascii: bool, out: &mut impl fmt::Write) -> fmt::Result {
    out.write_char('"')?;
    for c in text.chars() {
        match c {
            '"' => out.write_str("\\\"")?,
            '\\' => out.write_str("\\\\")?,
            '\n' => out.write_str("\\n")?,
            '\r' => out.write_str("\\r")?,
            '\t' => out.write_str("\\t")?,
            '\u{8}' => out.write_str("\\b")?,
            '\u{c}' => out.write_str("\\f")?,
            c if (c as u32) < 0x20 => push_unicode_escape(c as u32, out)?,
            c if ensure_ascii && !(' '..='~').contains(&c) => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    push_unicode_escape(u32::from(*unit), out)?;
                }
            }
            c => out.write_char(c)?,
        }
    }
    out.write_char('"')?;
    Ok(())
}

#[cfg(test)]
mod prefix_tests {
    use super::*;

    #[test]
    fn writer_stops_string_and_container_traversal_at_the_budget() {
        struct CountingWriter {
            calls: usize,
        }
        impl fmt::Write for CountingWriter {
            fn write_str(&mut self, _: &str) -> fmt::Result {
                self.calls += 1;
                if self.calls > 200 {
                    Err(fmt::Error)
                } else {
                    Ok(())
                }
            }
        }
        let values = [
            PyJson::Str("é".repeat(1_000_000)),
            PyJson::List(vec![PyJson::List(vec![PyJson::Null]); 100_000]),
            PyJson::Dict(
                (0..100_000)
                    .map(|n| (n.to_string(), PyJson::Null))
                    .collect(),
            ),
        ];
        for value in values {
            let mut writer = CountingWriter { calls: 0 };
            assert!(dump_to(&value, &DumpOpts::bounded_actual(), &mut writer).is_err());
            assert_eq!(writer.calls, 201);
            let (prefix, truncated) = actual_prefix(&value, 200);
            assert!(truncated);
            assert_eq!(prefix.chars().count(), 200);
        }
        // A disallowed float beyond the admitted prefix is never visited.
        let value = PyJson::List(vec![
            PyJson::from("x".repeat(1000)),
            PyJson::Float(f64::NAN),
        ]);
        let mut writer = CountingWriter { calls: 0 };
        assert_eq!(
            dump_to(&value, &DumpOpts::httpx_json(), &mut writer)
                .unwrap_err()
                .0,
            "JSON output writer stopped"
        );
    }

    #[test]
    fn prefixes_preserve_small_values_unicode_escapes_and_nonfinite_diagnostics() {
        for value in [
            PyJson::from("é\n🦀"),
            PyJson::Float(f64::NAN),
            PyJson::Float(f64::INFINITY),
        ] {
            let rendered = dumps(&value, &DumpOpts::bounded_actual()).unwrap();
            assert_eq!(
                actual_prefix(&value, rendered.chars().count()),
                (rendered.clone(), false)
            );
            assert_eq!(
                actual_prefix(&value, 2),
                (rendered.chars().take(2).collect(), true)
            );
        }
    }
}
