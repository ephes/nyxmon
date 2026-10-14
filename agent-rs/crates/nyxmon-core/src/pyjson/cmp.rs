//! Python truthiness and rich comparison over JSON values.

use std::cmp::Ordering;
use std::fmt;

use num_bigint::BigInt;
use num_traits::{FromPrimitive, Zero};

use super::PyJson;

/// A comparison operator of the `operator` module.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum CmpOp {
    Eq,
    Ne,
    Lt,
    Le,
    Gt,
    Ge,
}

impl CmpOp {
    pub const ALL: [CmpOp; 6] = [
        CmpOp::Eq,
        CmpOp::Ne,
        CmpOp::Lt,
        CmpOp::Le,
        CmpOp::Gt,
        CmpOp::Ge,
    ];

    /// The Python operator symbol (`"=="`, `"<"`, ...).
    pub fn symbol(self) -> &'static str {
        match self {
            CmpOp::Eq => "==",
            CmpOp::Ne => "!=",
            CmpOp::Lt => "<",
            CmpOp::Le => "<=",
            CmpOp::Gt => ">",
            CmpOp::Ge => ">=",
        }
    }

    /// Parse an operator symbol.
    pub fn from_symbol(symbol: &str) -> Option<CmpOp> {
        CmpOp::ALL.into_iter().find(|op| op.symbol() == symbol)
    }

    fn holds(self, ordering: Option<Ordering>) -> bool {
        match (self, ordering) {
            (CmpOp::Ne, None) => true,
            (_, None) => false,
            (CmpOp::Eq, Some(o)) => o == Ordering::Equal,
            (CmpOp::Ne, Some(o)) => o != Ordering::Equal,
            (CmpOp::Lt, Some(o)) => o == Ordering::Less,
            (CmpOp::Le, Some(o)) => o != Ordering::Greater,
            (CmpOp::Gt, Some(o)) => o == Ordering::Greater,
            (CmpOp::Ge, Some(o)) => o != Ordering::Less,
        }
    }
}

/// Python's `TypeError` for an unsupported ordering comparison.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TypeError(pub String);

impl fmt::Display for TypeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for TypeError {}

fn type_name(value: &PyJson) -> &'static str {
    match value {
        PyJson::Null => "NoneType",
        PyJson::Bool(_) => "bool",
        PyJson::Int(_) | PyJson::BigInt(_) => "int",
        PyJson::Float(_) => "float",
        PyJson::Str(_) => "str",
        PyJson::List(_) => "list",
        PyJson::Dict(_) => "dict",
    }
}

/// `notification_suppression.py:_json_kind`.
pub fn json_kind(value: &PyJson) -> &'static str {
    match value {
        PyJson::Null => "null",
        PyJson::Bool(_) => "bool",
        PyJson::Int(_) | PyJson::BigInt(_) | PyJson::Float(_) => "number",
        PyJson::Str(_) => "string",
        PyJson::List(_) => "list",
        PyJson::Dict(_) => "dict",
    }
}

/// `bool(value)`.
pub fn py_truthy(value: &PyJson) -> bool {
    match value {
        PyJson::Null => false,
        PyJson::Bool(flag) => *flag,
        PyJson::Int(number) => *number != 0,
        PyJson::BigInt(number) => !number.is_zero(),
        // NaN is truthy in Python.
        PyJson::Float(number) => *number != 0.0,
        PyJson::Str(text) => !text.is_empty(),
        PyJson::List(items) => !items.is_empty(),
        PyJson::Dict(map) => !map.is_empty(),
    }
}

enum Number {
    Int(BigInt),
    Float(f64),
}

fn as_number(value: &PyJson) -> Option<Number> {
    match value {
        PyJson::Bool(flag) => Some(Number::Int(BigInt::from(u8::from(*flag)))),
        PyJson::Int(number) => Some(Number::Int(BigInt::from(*number))),
        PyJson::BigInt(number) => Some(Number::Int(number.clone())),
        PyJson::Float(number) => Some(Number::Float(*number)),
        _ => None,
    }
}

/// Exact `int` <=> `float` comparison (CPython `float_richcompare`); `None`
/// when the float is NaN.
fn cmp_int_float(int: &BigInt, float: f64) -> Option<Ordering> {
    if float.is_nan() {
        return None;
    }
    if float.is_infinite() {
        return Some(if float > 0.0 {
            Ordering::Less
        } else {
            Ordering::Greater
        });
    }
    let whole = float.trunc();
    let whole_int = BigInt::from_f64(whole)?;
    Some(match int.cmp(&whole_int) {
        Ordering::Equal => {
            let fraction = float - whole;
            if fraction > 0.0 {
                Ordering::Less
            } else if fraction < 0.0 {
                Ordering::Greater
            } else {
                Ordering::Equal
            }
        }
        other => other,
    })
}

fn cmp_numbers(a: &Number, b: &Number) -> Option<Ordering> {
    match (a, b) {
        (Number::Int(x), Number::Int(y)) => Some(x.cmp(y)),
        (Number::Float(x), Number::Float(y)) => x.partial_cmp(y),
        (Number::Int(x), Number::Float(y)) => cmp_int_float(x, *y),
        (Number::Float(x), Number::Int(y)) => cmp_int_float(y, *x).map(Ordering::reverse),
    }
}

/// `PyObject_RichCompareBool(a, b, Py_EQ)` as used inside containers.
///
/// It short-cuts on object identity. Every `NaN` (and `Infinity`) that
/// `json.loads` produces is the same constant object
/// (`json.decoder.NaN`), so two NaNs compare equal *inside* lists and dicts,
/// while a top-level `NaN == NaN` is false. Iterative, so nesting depth is
/// bounded by memory, not by the thread stack.
fn contained_eq(a: &PyJson, b: &PyJson) -> bool {
    let mut pending = vec![(a, b)];
    while let Some((x, y)) = pending.pop() {
        let equal = match (x, y) {
            (PyJson::Float(p), PyJson::Float(q)) if p.is_nan() && q.is_nan() => true,
            (PyJson::List(p), PyJson::List(q)) => {
                if p.len() != q.len() {
                    return false;
                }
                pending.extend(p.iter().zip(q.iter()));
                true
            }
            (PyJson::Dict(p), PyJson::Dict(q)) => {
                if p.len() != q.len() {
                    return false;
                }
                for (key, value) in p {
                    match q.get(key) {
                        Some(other) => pending.push((value, other)),
                        None => return false,
                    }
                }
                true
            }
            _ => matches!(py_cmp(CmpOp::Eq, x, y), Ok(true)),
        };
        if !equal {
            return false;
        }
    }
    true
}

fn unsupported(op: CmpOp, a: &PyJson, b: &PyJson) -> TypeError {
    TypeError(format!(
        "'{}' not supported between instances of '{}' and '{}'",
        op.symbol(),
        type_name(a),
        type_name(b)
    ))
}

/// `operator.<op>(a, b)` with CPython semantics for JSON values: bool is an
/// int, int/float compare exactly, NaN is unordered, strings compare by code
/// point, lists lexicographically, dicts only for (deep) equality, and other
/// mixed types are unequal and unordered.
///
/// # Errors
/// [`TypeError`] for an ordering comparison Python does not support.
pub fn py_cmp(op: CmpOp, a: &PyJson, b: &PyJson) -> Result<bool, TypeError> {
    let equality = matches!(op, CmpOp::Eq | CmpOp::Ne);
    let (mut a, mut b) = (a, b);
    loop {
        if let (Some(x), Some(y)) = (as_number(a), as_number(b)) {
            return Ok(op.holds(cmp_numbers(&x, &y)));
        }
        return match (a, b) {
            (PyJson::Str(x), PyJson::Str(y)) => Ok(op.holds(Some(x.cmp(y)))),
            (PyJson::Null, PyJson::Null) if equality => Ok(op.holds(Some(Ordering::Equal))),
            (PyJson::List(x), PyJson::List(y)) => {
                if equality && x.len() != y.len() {
                    return Ok(op == CmpOp::Ne);
                }
                match x
                    .iter()
                    .zip(y.iter())
                    .position(|(p, q)| !contained_eq(p, q))
                {
                    None => Ok(op.holds(Some(x.len().cmp(&y.len())))),
                    Some(_) if equality => Ok(op == CmpOp::Ne),
                    Some(index) => {
                        // The first differing items decide the ordering.
                        (a, b) = (&x[index], &y[index]);
                        continue;
                    }
                }
            }
            // Values are compared like container elements (identity first).
            (PyJson::Dict(_), PyJson::Dict(_)) if equality => {
                Ok(contained_eq(a, b) == (op == CmpOp::Eq))
            }
            _ if equality => Ok(op == CmpOp::Ne),
            _ => Err(unsupported(op, a, b)),
        };
    }
}
