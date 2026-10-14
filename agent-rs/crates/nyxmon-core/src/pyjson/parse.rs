//! `json.loads` equivalent of CPython's C scanner (`Modules/_json.c`).
//!
//! Positions in error messages are code-point indices, as in Python. The
//! scanner accepts `NaN`, `Infinity` and `-Infinity`, parses integers of any
//! size up to CPython's 4300-digit conversion limit, lets duplicate keys keep
//! the first key's position with the last value, and rejects lone UTF-16
//! surrogates (PLAN.md D-8) only after the rest of the document parsed, so
//! every document Python rejects is rejected with Python's error.

use std::fmt;

use num_bigint::BigInt;

use super::{PyDict, PyJson};

/// CPython's default `sys.int_info.default_max_str_digits`.
pub(crate) const MAX_INT_STR_DIGITS: usize = 4300;

/// Deepest container nesting that is accepted.
///
/// CPython raises `RecursionError` when the C recursion budget runs out; from
/// a shallow call stack that happens at 9999 nested containers (CPython
/// 3.13). The exact Python boundary depends on the caller's stack depth, so
/// only documents far from it are pinned by fixtures.
pub const MAX_NESTING_DEPTH: usize = 9998;

/// Placeholder for a lone surrogate decoded from bytes (`surrogatepass`).
const SURROGATE_PLACEHOLDER: char = '\u{FFFD}';

/// Why a document was rejected. `Display` renders Python's `str(exc)`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LoadError {
    /// `json.JSONDecodeError` (a `ValueError`).
    Decode {
        msg: &'static str,
        pos: usize,
        lineno: usize,
        colno: usize,
    },
    /// `ValueError` from the integer string conversion limit.
    IntTooLong { digits: usize },
    /// `UnicodeDecodeError` while decoding bytes (message is P2).
    Unicode {
        encoding: &'static str,
        detail: String,
    },
    /// A lone UTF-16 surrogate inside a string (PLAN.md D-8). Python would
    /// accept the document; Rust strings cannot hold the value.
    LoneSurrogate { pos: usize },
    /// `RecursionError` for documents nested deeper than
    /// [`MAX_NESTING_DEPTH`].
    TooDeep,
}

impl LoadError {
    /// The Python exception class Python raises for the same input.
    /// [`LoadError::LoneSurrogate`] reports `ValueError`, the class the
    /// callers treat it as (D-8).
    pub fn python_class(&self) -> &'static str {
        match self {
            LoadError::Decode { .. } => "JSONDecodeError",
            LoadError::IntTooLong { .. } | LoadError::LoneSurrogate { .. } => "ValueError",
            LoadError::Unicode { .. } => "UnicodeDecodeError",
            LoadError::TooDeep => "RecursionError",
        }
    }

    /// Whether Python code catching `ValueError` catches this error.
    pub fn is_value_error(&self) -> bool {
        !matches!(self, LoadError::TooDeep)
    }
}

impl fmt::Display for LoadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            LoadError::Decode {
                msg,
                pos,
                lineno,
                colno,
            } => write!(f, "{msg}: line {lineno} column {colno} (char {pos})"),
            LoadError::IntTooLong { digits } => write!(
                f,
                "Exceeds the limit ({MAX_INT_STR_DIGITS} digits) for integer string \
                 conversion: value has {digits} digits; use sys.set_int_max_str_digits() \
                 to increase the limit"
            ),
            LoadError::Unicode { encoding, detail } => {
                write!(f, "'{encoding}' codec can't decode bytes: {detail}")
            }
            LoadError::LoneSurrogate { pos } => write!(
                f,
                "lone surrogate in a JSON string at char {pos} is not supported"
            ),
            LoadError::TooDeep => f.write_str(
                "maximum recursion depth exceeded while decoding a JSON document from a \
                 unicode string",
            ),
        }
    }
}

impl std::error::Error for LoadError {}

/// `json.loads(text)` for a `str` argument.
///
/// # Errors
/// The error Python raises for the same text (see [`LoadError`]).
pub fn loads(text: &str) -> Result<PyJson, LoadError> {
    let chars: Vec<char> = text.chars().collect();
    if chars.first() == Some(&'\u{FEFF}') {
        return Err(decode_error(
            &chars,
            "Unexpected UTF-8 BOM (decode using utf-8-sig)",
            0,
        ));
    }
    decode(&chars, &[])
}

/// `json.loads(data)` for a `bytes` argument: `json.detect_encoding`, then
/// `bytes.decode(encoding, "surrogatepass")`, then the scanner.
///
/// # Errors
/// The error Python raises for the same bytes (see [`LoadError`]).
pub fn loads_bytes(data: &[u8]) -> Result<PyJson, LoadError> {
    let (chars, surrogates) = decode_bytes(data)?;
    decode(&chars, &surrogates)
}

fn decode(chars: &[char], raw_surrogates: &[usize]) -> Result<PyJson, LoadError> {
    let mut scanner = Scanner {
        s: chars,
        lone_surrogate: None,
    };
    let start = scanner.skip_ws(0);
    let (value, end) = scanner.scan_once(start)?;
    let end = scanner.skip_ws(end);
    if end != chars.len() {
        return Err(decode_error(chars, "Extra data", end));
    }
    let surrogate = match (scanner.lone_surrogate, raw_surrogates.first()) {
        (Some(a), Some(&b)) => Some(a.min(b)),
        (a, b) => a.or(b.copied()),
    };
    if let Some(pos) = surrogate {
        return Err(LoadError::LoneSurrogate { pos });
    }
    Ok(value)
}

fn decode_error(s: &[char], msg: &'static str, pos: usize) -> LoadError {
    let before = &s[..pos.min(s.len())];
    let lineno = before.iter().filter(|&&c| c == '\n').count() + 1;
    let colno = match before.iter().rposition(|&c| c == '\n') {
        Some(newline) => pos - newline,
        None => pos + 1,
    };
    LoadError::Decode {
        msg,
        pos,
        lineno,
        colno,
    }
}

struct Scanner<'a> {
    s: &'a [char],
    lone_surrogate: Option<usize>,
}

/// An open container on the scanner's explicit stack.
enum Frame {
    Array(Vec<PyJson>),
    /// The dict so far and the key whose value is being read.
    Object(PyDict, String),
}

fn is_ws(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\n' | '\r')
}

impl Scanner<'_> {
    fn at(&self, idx: usize) -> Option<char> {
        self.s.get(idx).copied()
    }

    fn skip_ws(&self, mut idx: usize) -> usize {
        while self.at(idx).is_some_and(is_ws) {
            idx += 1;
        }
        idx
    }

    fn matches(&self, idx: usize, word: &str) -> bool {
        (idx..)
            .zip(word.chars())
            .all(|(i, expected)| self.at(i) == Some(expected))
    }

    fn err(&self, msg: &'static str, pos: usize) -> LoadError {
        decode_error(self.s, msg, pos)
    }

    /// A scalar at `idx` (`scan_once_unicode` minus the containers).
    fn scan_scalar(&mut self, idx: usize) -> Result<(PyJson, usize), LoadError> {
        let Some(c) = self.at(idx) else {
            return Err(self.err("Expecting value", idx));
        };
        match c {
            '"' => {
                let (text, end) = self.scan_string(idx + 1)?;
                return Ok((PyJson::Str(text), end));
            }
            'n' if self.matches(idx, "null") => return Ok((PyJson::Null, idx + 4)),
            't' if self.matches(idx, "true") => return Ok((PyJson::Bool(true), idx + 4)),
            'f' if self.matches(idx, "false") => return Ok((PyJson::Bool(false), idx + 5)),
            'N' if self.matches(idx, "NaN") => return Ok((PyJson::Float(f64::NAN), idx + 3)),
            'I' if self.matches(idx, "Infinity") => {
                return Ok((PyJson::Float(f64::INFINITY), idx + 8));
            }
            '-' if self.matches(idx, "-Infinity") => {
                return Ok((PyJson::Float(f64::NEG_INFINITY), idx + 9));
            }
            _ => {}
        }
        self.match_number(idx)
    }

    /// Read `"key"` and the `:` delimiter at `idx`; returns the key and the
    /// index of the value.
    fn scan_key(&mut self, idx: usize) -> Result<(String, usize), LoadError> {
        if self.at(idx) != Some('"') {
            return Err(self.err("Expecting property name enclosed in double quotes", idx));
        }
        let (key, next) = self.scan_string(idx + 1)?;
        let idx = self.skip_ws(next);
        if self.at(idx) != Some(':') {
            return Err(self.err("Expecting ':' delimiter", idx));
        }
        Ok((key, self.skip_ws(idx + 1)))
    }

    /// `scan_once_unicode` with the container recursion of
    /// `_parse_object_unicode`/`_parse_array_unicode` unrolled onto an
    /// explicit stack, so hostile nesting cannot exhaust the thread stack.
    fn scan_once(&mut self, start: usize) -> Result<(PyJson, usize), LoadError> {
        let mut stack: Vec<Frame> = Vec::new();
        let mut idx = start;
        loop {
            // Read one value at `idx`, descending into containers.
            let (mut value, mut end) = match self.at(idx) {
                Some(open @ ('{' | '[')) => {
                    if stack.len() >= MAX_NESTING_DEPTH {
                        return Err(LoadError::TooDeep);
                    }
                    let inner = self.skip_ws(idx + 1);
                    if open == '[' {
                        if self.at(inner) == Some(']') {
                            (PyJson::List(Vec::new()), inner + 1)
                        } else {
                            stack.push(Frame::Array(Vec::new()));
                            idx = inner;
                            continue;
                        }
                    } else if self.at(inner) == Some('}') {
                        (PyJson::Dict(PyDict::new()), inner + 1)
                    } else {
                        let (key, value_idx) = self.scan_key(inner)?;
                        stack.push(Frame::Object(PyDict::new(), key));
                        idx = value_idx;
                        continue;
                    }
                }
                _ => self.scan_scalar(idx)?,
            };
            // Attach the finished value to its parents, closing containers.
            loop {
                let Some(frame) = stack.last_mut() else {
                    return Ok((value, end));
                };
                let close = match frame {
                    Frame::Array(items) => {
                        items.push(value);
                        ']'
                    }
                    Frame::Object(map, key) => {
                        // Python dict assignment: a repeated key keeps its
                        // first position and takes the last value.
                        map.insert(std::mem::take(key), value);
                        '}'
                    }
                };
                let next = self.skip_ws(end);
                if self.at(next) == Some(close) {
                    value = match stack.pop() {
                        Some(Frame::Array(items)) => PyJson::List(items),
                        Some(Frame::Object(map, _)) => PyJson::Dict(map),
                        None => unreachable!("frame checked above"),
                    };
                    end = next + 1;
                    continue;
                }
                if self.at(next) != Some(',') {
                    return Err(self.err("Expecting ',' delimiter", next));
                }
                let after = self.skip_ws(next + 1);
                if self.at(after) == Some(close) {
                    let msg = if close == ']' {
                        "Illegal trailing comma before end of array"
                    } else {
                        "Illegal trailing comma before end of object"
                    };
                    return Err(self.err(msg, next));
                }
                idx = after;
                if let Some(Frame::Object(_, key)) = stack.last_mut() {
                    let (next_key, value_idx) = self.scan_key(after)?;
                    *key = next_key;
                    idx = value_idx;
                }
                break;
            }
        }
    }

    fn is_digit_at(&self, idx: usize) -> bool {
        self.at(idx).is_some_and(|c| c.is_ascii_digit())
    }

    fn match_number(&self, start: usize) -> Result<(PyJson, usize), LoadError> {
        let len = self.s.len();
        let mut idx = start;
        if self.at(idx) == Some('-') {
            idx += 1;
            if idx >= len {
                return Err(self.err("Expecting value", start));
            }
        }
        match self.at(idx) {
            Some('1'..='9') => {
                idx += 1;
                while self.is_digit_at(idx) {
                    idx += 1;
                }
            }
            Some('0') => idx += 1,
            _ => return Err(self.err("Expecting value", start)),
        }
        let mut is_float = false;
        if idx + 1 < len && self.s[idx] == '.' && self.is_digit_at(idx + 1) {
            is_float = true;
            idx += 2;
            while self.is_digit_at(idx) {
                idx += 1;
            }
        }
        if idx + 1 < len && matches!(self.s[idx], 'e' | 'E') {
            let e_start = idx;
            idx += 1;
            if idx + 1 < len && matches!(self.s[idx], '-' | '+') {
                idx += 1;
            }
            while self.is_digit_at(idx) {
                idx += 1;
            }
            if self.s[idx - 1].is_ascii_digit() {
                is_float = true;
            } else {
                idx = e_start;
            }
        }
        let numstr: String = self.s[start..idx].iter().collect();
        if is_float {
            // Rust's parser is correctly rounded like CPython's and also
            // overflows to +/-inf.
            let value: f64 = numstr
                .parse()
                .map_err(|_| self.err("Expecting value", start))?;
            return Ok((PyJson::Float(value), idx));
        }
        let digits = numstr.trim_start_matches('-').len();
        if digits > MAX_INT_STR_DIGITS {
            return Err(LoadError::IntTooLong { digits });
        }
        let value = match numstr.parse::<i64>() {
            Ok(small) => PyJson::Int(small),
            Err(_) => PyJson::from_bigint(
                numstr
                    .parse::<BigInt>()
                    .map_err(|_| self.err("Expecting value", start))?,
            ),
        };
        Ok((value, idx))
    }

    fn hex4(&self, from: usize) -> Option<u32> {
        let mut value = 0u32;
        for idx in from..from + 4 {
            value = (value << 4) | self.at(idx)?.to_digit(16)?;
        }
        Some(value)
    }

    /// `scanstring_unicode`; `begin` is the index after the opening quote.
    fn scan_string(&mut self, begin: usize) -> Result<(String, usize), LoadError> {
        let len = self.s.len();
        let quote = begin - 1;
        let mut out = String::new();
        let mut end = begin;
        loop {
            let mut next = end;
            let mut terminator = None;
            while next < len {
                let c = self.s[next];
                if c == '"' || c == '\\' {
                    terminator = Some(c);
                    break;
                }
                if (c as u32) <= 0x1f {
                    return Err(self.err("Invalid control character at", next));
                }
                next += 1;
            }
            let Some(terminator) = terminator else {
                return Err(self.err("Unterminated string starting at", quote));
            };
            out.extend(&self.s[end..next]);
            next += 1;
            if terminator == '"' {
                return Ok((out, next));
            }
            if next == len {
                return Err(self.err("Unterminated string starting at", quote));
            }
            let escape = self.s[next];
            if escape != 'u' {
                end = next + 1;
                let decoded = match escape {
                    '"' => '"',
                    '\\' => '\\',
                    '/' => '/',
                    'b' => '\u{8}',
                    'f' => '\u{c}',
                    'n' => '\n',
                    'r' => '\r',
                    't' => '\t',
                    _ => return Err(self.err("Invalid \\escape", end - 2)),
                };
                out.push(decoded);
                continue;
            }
            let backslash = next - 1;
            next += 1;
            end = next + 4;
            // Current CPython 3.13 (3.13.16 verified; 3.13.11 still differed):
            // four hex digits that end the input are decoded, so `"\u1234`
            // is an unterminated string rather than an invalid escape.
            if end > len {
                return Err(self.err("Invalid \\uXXXX escape", next - 1));
            }
            let Some(mut code) = self.hex4(next) else {
                return Err(self.err("Invalid \\uXXXX escape", end - 5));
            };
            if (0xD800..0xDC00).contains(&code)
                && end + 6 < len
                && self.s[end] == '\\'
                && self.s[end + 1] == 'u'
            {
                end += 6;
                let Some(low) = self.hex4(end - 4) else {
                    return Err(self.err("Invalid \\uXXXX escape", end - 5));
                };
                if (0xDC00..0xE000).contains(&low) {
                    code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
                } else {
                    end -= 6;
                }
            }
            match char::from_u32(code) {
                Some(c) => out.push(c),
                None => {
                    // A lone surrogate: Python keeps it, Rust cannot (D-8).
                    self.lone_surrogate.get_or_insert(backslash);
                    out.push(SURROGATE_PLACEHOLDER);
                }
            }
        }
    }
}

/// `json.detect_encoding`.
fn detect_encoding(b: &[u8]) -> &'static str {
    if b.starts_with(&[0x00, 0x00, 0xFE, 0xFF]) || b.starts_with(&[0xFF, 0xFE, 0x00, 0x00]) {
        return "utf-32";
    }
    if b.starts_with(&[0xFE, 0xFF]) || b.starts_with(&[0xFF, 0xFE]) {
        return "utf-16";
    }
    if b.starts_with(&[0xEF, 0xBB, 0xBF]) {
        return "utf-8-sig";
    }
    if b.len() >= 4 {
        if b[0] == 0 {
            return if b[1] != 0 { "utf-16-be" } else { "utf-32-be" };
        }
        if b[1] == 0 {
            return if b[2] != 0 || b[3] != 0 {
                "utf-16-le"
            } else {
                "utf-32-le"
            };
        }
    } else if b.len() == 2 {
        if b[0] == 0 {
            return "utf-16-be";
        }
        if b[1] == 0 {
            return "utf-16-le";
        }
    }
    "utf-8"
}

type Decoded = (Vec<char>, Vec<usize>);

/// Decode with `surrogatepass`: lone surrogates become a placeholder whose
/// position is reported separately.
fn decode_bytes(data: &[u8]) -> Result<Decoded, LoadError> {
    let encoding = detect_encoding(data);
    match encoding {
        "utf-8" => decode_utf8(data, encoding),
        "utf-8-sig" => decode_utf8(&data[3..], encoding),
        "utf-16" => {
            let little = data[0] == 0xFF;
            decode_utf16(&data[2..], little, encoding)
        }
        "utf-16-le" => decode_utf16(data, true, encoding),
        "utf-16-be" => decode_utf16(data, false, encoding),
        "utf-32" => {
            let little = data[0] == 0xFF;
            decode_utf32(&data[4..], little, encoding)
        }
        "utf-32-le" => decode_utf32(data, true, encoding),
        _ => decode_utf32(data, false, encoding),
    }
}

fn unicode_error(encoding: &'static str, detail: String) -> LoadError {
    LoadError::Unicode { encoding, detail }
}

fn decode_utf8(mut data: &[u8], encoding: &'static str) -> Result<Decoded, LoadError> {
    let mut chars = Vec::with_capacity(data.len());
    let mut surrogates = Vec::new();
    let mut offset = 0usize;
    loop {
        match std::str::from_utf8(data) {
            Ok(text) => {
                chars.extend(text.chars());
                return Ok((chars, surrogates));
            }
            Err(err) => {
                let valid = err.valid_up_to();
                let prefix = std::str::from_utf8(&data[..valid]).unwrap_or_default();
                chars.extend(prefix.chars());
                let rest = &data[valid..];
                // `surrogatepass` accepts UTF-8 encoded surrogates (ED A0..BF xx).
                if rest.len() >= 3
                    && rest[0] == 0xED
                    && (0xA0..=0xBF).contains(&rest[1])
                    && (0x80..=0xBF).contains(&rest[2])
                {
                    surrogates.push(chars.len());
                    chars.push(SURROGATE_PLACEHOLDER);
                    data = &rest[3..];
                    offset += valid + 3;
                    continue;
                }
                return Err(unicode_error(
                    encoding,
                    format!("invalid data in position {}", offset + valid),
                ));
            }
        }
    }
}

fn decode_utf16(data: &[u8], little: bool, encoding: &'static str) -> Result<Decoded, LoadError> {
    if !data.len().is_multiple_of(2) {
        return Err(unicode_error(encoding, "truncated data".to_owned()));
    }
    let units = data.as_chunks::<2>().0.iter().map(|pair| {
        if little {
            u16::from_le_bytes(*pair)
        } else {
            u16::from_be_bytes(*pair)
        }
    });
    let mut chars = Vec::with_capacity(data.len() / 2);
    let mut surrogates = Vec::new();
    for unit in char::decode_utf16(units) {
        match unit {
            Ok(c) => chars.push(c),
            Err(_) => {
                surrogates.push(chars.len());
                chars.push(SURROGATE_PLACEHOLDER);
            }
        }
    }
    Ok((chars, surrogates))
}

fn decode_utf32(data: &[u8], little: bool, encoding: &'static str) -> Result<Decoded, LoadError> {
    if !data.len().is_multiple_of(4) {
        return Err(unicode_error(encoding, "truncated data".to_owned()));
    }
    let mut chars = Vec::with_capacity(data.len() / 4);
    let mut surrogates = Vec::new();
    for (index, quad) in data.as_chunks::<4>().0.iter().enumerate() {
        let bytes = *quad;
        let code = if little {
            u32::from_le_bytes(bytes)
        } else {
            u32::from_be_bytes(bytes)
        };
        match char::from_u32(code) {
            Some(c) => chars.push(c),
            None if (0xD800..0xE000).contains(&code) => {
                surrogates.push(chars.len());
                chars.push(SURROGATE_PLACEHOLDER);
            }
            None => {
                return Err(unicode_error(
                    encoding,
                    format!(
                        "code point not in range(0x110000) in position {}",
                        index * 4
                    ),
                ));
            }
        }
    }
    Ok((chars, surrogates))
}
