//! `json.dumps` equivalent (CPython `Lib/json/encoder.py` and the C encoder).

use std::fmt::{self, Write as _};

use super::PyJson;
use super::num::py_float_repr;

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

/// `json.dumps(value, **opts)`.
///
/// Iterative, so arbitrarily nested values cannot exhaust the thread stack.
///
/// # Errors
/// [`DumpError`] for a non-finite float when `allow_nan` is false.
pub fn dumps(value: &PyJson, opts: &DumpOpts) -> Result<String, DumpError> {
    let mut out = String::new();
    let mut tasks = vec![Task::Value(value, 0)];
    while let Some(task) = tasks.pop() {
        match task {
            Task::Separator(level) => {
                out.push_str(opts.item_separator);
                newline_indent(opts, level, &mut out);
            }
            Task::Key(key) => {
                encode_str(key, opts.ensure_ascii, &mut out);
                out.push_str(opts.key_separator);
            }
            Task::Close(bracket, level) => {
                newline_indent(opts, level, &mut out);
                out.push(bracket);
            }
            Task::Value(value, level) => match value {
                PyJson::Null => out.push_str("null"),
                PyJson::Bool(true) => out.push_str("true"),
                PyJson::Bool(false) => out.push_str("false"),
                PyJson::Int(number) => {
                    let _ = write!(out, "{number}");
                }
                PyJson::BigInt(number) => {
                    let _ = write!(out, "{number}");
                }
                PyJson::Float(number) => encode_float(*number, opts, &mut out)?,
                PyJson::Str(text) => encode_str(text, opts.ensure_ascii, &mut out),
                PyJson::List(items) if items.is_empty() => out.push_str("[]"),
                PyJson::Dict(map) if map.is_empty() => out.push_str("{}"),
                PyJson::List(items) => {
                    out.push('[');
                    newline_indent(opts, level + 1, &mut out);
                    tasks.push(Task::Close(']', level));
                    for (index, item) in items.iter().enumerate().rev() {
                        tasks.push(Task::Value(item, level + 1));
                        if index > 0 {
                            tasks.push(Task::Separator(level + 1));
                        }
                    }
                }
                PyJson::Dict(map) => {
                    let mut entries: Vec<(&String, &PyJson)> = map.iter().collect();
                    if opts.sort_keys {
                        entries.sort_by(|a, b| a.0.cmp(b.0));
                    }
                    out.push('{');
                    newline_indent(opts, level + 1, &mut out);
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
    Ok(out)
}

/// Pending output of [`dumps`], popped from the end.
enum Task<'a> {
    Value(&'a PyJson, usize),
    Key(&'a str),
    /// Item separator, then the newline/indent of the given level.
    Separator(usize),
    /// Newline/indent of the given level, then the closing bracket.
    Close(char, usize),
}

fn newline_indent(opts: &DumpOpts, level: usize, out: &mut String) {
    if let Some(width) = opts.indent {
        out.push('\n');
        out.extend(std::iter::repeat_n(' ', width * level));
    }
}

fn encode_float(number: f64, opts: &DumpOpts, out: &mut String) -> Result<(), DumpError> {
    if number.is_finite() {
        out.push_str(&py_float_repr(number));
        return Ok(());
    }
    if !opts.allow_nan {
        return Err(DumpError(format!(
            "Out of range float values are not JSON compliant: {}",
            py_float_repr(number)
        )));
    }
    out.push_str(if number.is_nan() {
        "NaN"
    } else if number > 0.0 {
        "Infinity"
    } else {
        "-Infinity"
    });
    Ok(())
}

fn push_unicode_escape(unit: u32, out: &mut String) {
    let _ = write!(out, "\\u{unit:04x}");
}

/// `py_encode_basestring_ascii` / `py_encode_basestring`.
pub(crate) fn encode_str(text: &str, ensure_ascii: bool, out: &mut String) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => push_unicode_escape(c as u32, out),
            c if ensure_ascii && !(' '..='~').contains(&c) => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    push_unicode_escape(u32::from(*unit), out);
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}
