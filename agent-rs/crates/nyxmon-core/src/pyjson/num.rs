//! Python numeric built-ins: `repr(float)`, `format(x, "g")`, `round`,
//! `int()` and `float()`.
//!
//! Rust's float formatting is exact (correctly rounded, ties to even on the
//! exact binary value) like CPython's `dtoa`, so the decimal digits are taken
//! from `format!` and laid out with Python's rules.

use std::fmt;

use num_bigint::BigInt;
use num_traits::{FromPrimitive, ToPrimitive};

use super::PyJson;
use super::parse::MAX_INT_STR_DIGITS;
use super::repr::{py_isspace, py_str_repr};

/// The whitespace `int()` and `float()` strip from a `str` argument: ASCII
/// `Py_ISSPACE` (` \t\n\v\f\r`, not U+001C..U+001F) plus every non-ASCII
/// Unicode whitespace character
/// (`_PyUnicode_TransformDecimalAndSpaceToASCII`).
fn strip_numeric(text: &str) -> &str {
    text.trim_matches(|c: char| {
        if c.is_ascii() {
            matches!(c, ' ' | '\t' | '\n' | '\u{b}' | '\u{c}' | '\r')
        } else {
            py_isspace(c)
        }
    })
}

/// A Python exception raised by a numeric conversion.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PyNumError {
    ValueError(String),
    TypeError(String),
    OverflowError(String),
}

impl PyNumError {
    pub fn python_class(&self) -> &'static str {
        match self {
            PyNumError::ValueError(_) => "ValueError",
            PyNumError::TypeError(_) => "TypeError",
            PyNumError::OverflowError(_) => "OverflowError",
        }
    }
}

impl fmt::Display for PyNumError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            PyNumError::ValueError(msg)
            | PyNumError::TypeError(msg)
            | PyNumError::OverflowError(msg) => f.write_str(msg),
        }
    }
}

impl std::error::Error for PyNumError {}

/// Split Rust's `{:e}` output into (negative, digits, decimal exponent).
fn split_sci(sci: &str) -> (bool, String, i32) {
    let (negative, rest) = match sci.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, sci),
    };
    let (mantissa, exponent) = rest.split_once('e').unwrap_or((rest, "0"));
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    (negative, digits, exponent.parse().unwrap_or(0))
}

/// The shortest digits that round-trip, choosing the candidate nearest to
/// `x` (ties to even) like CPython's `dtoa` mode 0. Rust's shortest
/// formatting finds the length but may break a tie the other way, so the
/// correctly rounded decimal of that length is preferred when it round-trips.
fn shortest_digits(x: f64) -> (bool, String, i32) {
    let (negative, digits, exponent) = split_sci(&format!("{x:e}"));
    let nearest = format!("{:.*e}", digits.len() - 1, x);
    if nearest.parse::<f64>().ok() == Some(x) {
        return split_sci(&nearest);
    }
    (negative, digits, exponent)
}

fn push_exponent(out: &mut String, exponent: i32) {
    out.push('e');
    out.push(if exponent < 0 { '-' } else { '+' });
    out.push_str(&format!("{:02}", exponent.unsigned_abs()));
}

/// `repr(x)` for a float (CPython's shortest round-trip repr).
pub fn py_float_repr(x: f64) -> String {
    if x.is_nan() {
        return "nan".to_owned();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0.0" } else { "0.0" }.to_owned();
    }
    let (negative, digits, exponent) = shortest_digits(x);
    let decpt = exponent + 1;
    let mut out = String::new();
    if negative {
        out.push('-');
    }
    if decpt > -4 && decpt <= 16 {
        if decpt <= 0 {
            out.push_str("0.");
            out.extend(std::iter::repeat_n('0', decpt.unsigned_abs() as usize));
            out.push_str(&digits);
        } else {
            let point = decpt as usize;
            if point >= digits.len() {
                out.push_str(&digits);
                out.extend(std::iter::repeat_n('0', point - digits.len()));
                out.push_str(".0");
            } else {
                out.push_str(&digits[..point]);
                out.push('.');
                out.push_str(&digits[point..]);
            }
        }
    } else {
        out.push_str(&digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        push_exponent(&mut out, exponent);
    }
    out
}

fn strip_fraction_zeros(text: &str) -> &str {
    if text.contains('.') {
        text.trim_end_matches('0').trim_end_matches('.')
    } else {
        text
    }
}

/// `format(x, "g")` (precision 6, trailing zeros removed).
pub fn py_format_g(x: f64) -> String {
    const PRECISION: i32 = 6;
    if x.is_nan() {
        return "nan".to_owned();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    let sci = format!("{:.*e}", (PRECISION - 1) as usize, x);
    let (negative, digits, exponent) = split_sci(&sci);
    if (-4..PRECISION).contains(&exponent) {
        let fixed = format!("{:.*}", (PRECISION - 1 - exponent) as usize, x);
        return strip_fraction_zeros(&fixed).to_owned();
    }
    let mut out = String::new();
    if negative {
        out.push('-');
    }
    let mut mantissa = String::from(&digits[..1]);
    mantissa.push('.');
    mantissa.push_str(&digits[1..]);
    out.push_str(strip_fraction_zeros(&mantissa));
    push_exponent(&mut out, exponent);
    out
}

/// `round(x, ndigits)` for a float and `ndigits >= 0` (correctly rounded,
/// ties to even on the exact binary value, like CPython).
pub fn py_round(x: f64, ndigits: u32) -> f64 {
    if !x.is_finite() || x == 0.0 || ndigits > 323 {
        return x;
    }
    format!("{:.*}", ndigits as usize, x).parse().unwrap_or(x)
}

fn invalid_int(text: &str) -> PyNumError {
    // CPython formats the literal with `%.200R`: its repr cut to 200
    // characters (the closing quote included in the count).
    let literal: String = py_str_repr(text).chars().take(200).collect();
    PyNumError::ValueError(format!("invalid literal for int() with base 10: {literal}"))
}

/// Digits with single underscores between them (PEP 515), ASCII only (D-6).
fn valid_digit_run(body: &str) -> bool {
    let bytes = body.as_bytes();
    !bytes.is_empty()
        && bytes[0].is_ascii_digit()
        && bytes[bytes.len() - 1].is_ascii_digit()
        && bytes.iter().all(|b| b.is_ascii_digit() || *b == b'_')
        && !body.contains("__")
}

/// `int(text)` for a `str` (base 10).
///
/// # Errors
/// `ValueError` for anything Python's `int()` rejects, including more than
/// 4300 digits; non-ASCII digits are rejected too (D-6).
pub fn py_int(text: &str) -> Result<BigInt, PyNumError> {
    let stripped = strip_numeric(text);
    let (negative, body) = match stripped.as_bytes().first() {
        Some(b'-') => (true, &stripped[1..]),
        Some(b'+') => (false, &stripped[1..]),
        _ => (false, stripped),
    };
    if !valid_digit_run(body) {
        return Err(invalid_int(text));
    }
    let digits: String = body.chars().filter(|c| *c != '_').collect();
    if digits.len() > MAX_INT_STR_DIGITS {
        return Err(PyNumError::ValueError(format!(
            "Exceeds the limit ({MAX_INT_STR_DIGITS} digits) for integer string conversion: \
             value has {} digits; use sys.set_int_max_str_digits() to increase the limit",
            digits.len()
        )));
    }
    let magnitude: BigInt = digits.parse().map_err(|_| invalid_int(text))?;
    Ok(if negative { -magnitude } else { magnitude })
}

fn invalid_float(text: &str) -> PyNumError {
    PyNumError::ValueError(format!(
        "could not convert string to float: {}",
        py_str_repr(text)
    ))
}

fn valid_decimal(text: &str) -> bool {
    let body = text.strip_prefix(['+', '-']).unwrap_or(text);
    let lower = body.to_ascii_lowercase();
    if matches!(lower.as_str(), "inf" | "infinity" | "nan") {
        return true;
    }
    let (mantissa, exponent) = match body.find(['e', 'E']) {
        Some(at) => (&body[..at], Some(&body[at + 1..])),
        None => (body, None),
    };
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let all_digits = |s: &str| s.bytes().all(|b| b.is_ascii_digit());
    if !all_digits(whole) || !all_digits(fraction) || whole.len() + fraction.len() == 0 {
        return false;
    }
    match exponent {
        None => true,
        Some(exp) => {
            let digits = exp.strip_prefix(['+', '-']).unwrap_or(exp);
            !digits.is_empty() && all_digits(digits)
        }
    }
}

/// `float(text)` for a `str`.
///
/// # Errors
/// `ValueError` for anything Python's `float()` rejects (non-ASCII digits
/// too, D-6). Overflowing values become `inf` as in Python.
pub fn py_float(text: &str) -> Result<f64, PyNumError> {
    let stripped = strip_numeric(text);
    let bytes = stripped.as_bytes();
    for (index, byte) in bytes.iter().enumerate() {
        if *byte == b'_' {
            let before = index > 0 && bytes[index - 1].is_ascii_digit();
            let after = bytes.get(index + 1).is_some_and(u8::is_ascii_digit);
            if !(before && after) {
                return Err(invalid_float(text));
            }
        }
    }
    let cleaned: String = stripped.chars().filter(|c| *c != '_').collect();
    if !valid_decimal(&cleaned) {
        return Err(invalid_float(text));
    }
    cleaned.parse().map_err(|_| invalid_float(text))
}

/// `int(value)` for a JSON value: bools are 0/1, floats truncate, strings
/// parse with [`py_int`].
///
/// # Errors
/// `TypeError` for null, lists and dicts; `ValueError` for NaN and invalid
/// strings; `OverflowError` for infinities.
pub fn py_int_of(value: &PyJson) -> Result<BigInt, PyNumError> {
    let type_error = |name: &str| {
        PyNumError::TypeError(format!(
            "int() argument must be a string, a bytes-like object or a real number, not '{name}'"
        ))
    };
    match value {
        PyJson::Bool(flag) => Ok(BigInt::from(u8::from(*flag))),
        PyJson::Int(number) => Ok(BigInt::from(*number)),
        PyJson::BigInt(number) => Ok(number.clone()),
        PyJson::Float(number) if number.is_nan() => Err(PyNumError::ValueError(
            "cannot convert float NaN to integer".to_owned(),
        )),
        PyJson::Float(number) if number.is_infinite() => Err(PyNumError::OverflowError(
            "cannot convert float infinity to integer".to_owned(),
        )),
        PyJson::Float(number) => BigInt::from_f64(number.trunc())
            .ok_or_else(|| PyNumError::ValueError("cannot convert float to integer".to_owned())),
        PyJson::Str(text) => py_int(text),
        PyJson::Null => Err(type_error("NoneType")),
        PyJson::List(_) => Err(type_error("list")),
        PyJson::Dict(_) => Err(type_error("dict")),
    }
}

/// Clamp an arbitrary integer into `i64`.
pub fn bigint_to_i64_saturating(value: &BigInt) -> i64 {
    value
        .to_i64()
        .unwrap_or(if value.sign() == num_bigint::Sign::Minus {
            i64::MIN
        } else {
            i64::MAX
        })
}

/// `float(n)` for a Python `int`: correctly rounded (half to even).
///
/// # Errors
/// `OverflowError` ("int too large to convert to float") when the rounded
/// value does not fit in a double, like CPython.
pub fn py_float_of_int(value: &BigInt) -> Result<f64, PyNumError> {
    let converted = match value.to_i64() {
        // Exact below 2**53 and correctly rounded above, like the decimal
        // parse below.
        Some(small) => small as f64,
        None => value.to_string().parse::<f64>().unwrap_or(f64::INFINITY),
    };
    if converted.is_finite() {
        Ok(converted)
    } else {
        Err(PyNumError::OverflowError(
            "int too large to convert to float".to_owned(),
        ))
    }
}
