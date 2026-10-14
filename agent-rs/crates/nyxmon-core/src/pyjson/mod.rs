//! CPython-compatible JSON values (PLAN.md section 4.2).
//!
//! [`PyJson`] is the value type for every JSON document the agent reads or
//! writes. It keeps dict insertion order, distinguishes `int` from `float`,
//! holds integers beyond 64 bits exactly and allows `NaN`/`Infinity`, so that
//! [`loads`]/[`dumps`] round-trip exactly like CPython's `json` module.
//!
//! The helpers reproduce the Python built-ins the ported code relies on:
//! `repr(float)`, `format(x, "g")`, `round`, `int()`, `float()`, truthiness,
//! rich comparison, `repr(str)`, `repr(bytes)` and `repr()`/`str()` of values.

mod cmp;
mod dump;
mod num;
mod parse;
mod props;
mod repr;

pub use cmp::{CmpOp, TypeError, json_kind, py_cmp, py_truthy, type_name as py_type_name};
pub use dump::{DumpError, DumpOpts, dumps};
pub use num::{
    PyNumError, bigint_to_i64_saturating, py_float, py_float_of_int, py_float_repr, py_format_g,
    py_int, py_int_of, py_round,
};
pub use parse::{LoadError, MAX_NESTING_DEPTH, loads, loads_bytes};
pub use props::{nfkc_adds_url_delimiter, py_isdigit, py_lower};
pub use repr::{char_len, py_bytes_repr, py_isspace, py_repr, py_str, py_str_repr, py_strip};

use indexmap::IndexMap;
use num_bigint::BigInt;
use num_traits::ToPrimitive;

/// A JSON object with Python dict semantics (insertion order kept; assigning
/// an existing key keeps its position).
pub type PyDict = IndexMap<String, PyJson>;

/// A JSON value as CPython's `json` module produces it.
///
/// Invariant: [`PyJson::BigInt`] only holds values outside the `i64` range;
/// use [`PyJson::from_bigint`] to construct integers of unknown size.
///
/// `PartialEq` is *structural identity* (same variant, floats compared by bit
/// pattern, so `NaN == NaN` and `0.0 != -0.0`, and `1 != 1.0`). It is meant
/// for tests and bookkeeping. Python's `==` is [`py_cmp`] with
/// [`CmpOp::Eq`].
///
/// Documents from monitored endpoints may be nested up to
/// [`MAX_NESTING_DEPTH`] levels. Parsing, serialising, comparing, cloning,
/// structural equality and dropping are therefore iterative and never
/// recurse per nesting level (only the derived `Debug` does). Because
/// `PyJson` implements `Drop`, move container contents out with
/// [`PyJson::into_dict`]/[`PyJson::into_list`]/[`PyJson::into_string`]
/// instead of destructuring by value.
#[derive(Debug)]
pub enum PyJson {
    Null,
    Bool(bool),
    Int(i64),
    BigInt(BigInt),
    Float(f64),
    Str(String),
    List(Vec<PyJson>),
    Dict(PyDict),
}

impl PyJson {
    /// An integer of any size, normalised to [`PyJson::Int`] when it fits.
    pub fn from_bigint(value: BigInt) -> PyJson {
        match value.to_i64() {
            Some(small) => PyJson::Int(small),
            None => PyJson::BigInt(value),
        }
    }

    /// An empty dict.
    pub fn dict() -> PyJson {
        PyJson::Dict(PyDict::new())
    }

    pub fn is_null(&self) -> bool {
        matches!(self, PyJson::Null)
    }

    pub fn as_dict(&self) -> Option<&PyDict> {
        match self {
            PyJson::Dict(map) => Some(map),
            _ => None,
        }
    }

    pub fn as_dict_mut(&mut self) -> Option<&mut PyDict> {
        match self {
            PyJson::Dict(map) => Some(map),
            _ => None,
        }
    }

    pub fn as_list(&self) -> Option<&[PyJson]> {
        match self {
            PyJson::List(items) => Some(items),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            PyJson::Str(text) => Some(text),
            _ => None,
        }
    }

    /// `isinstance(value, bool)`.
    pub fn as_bool(&self) -> Option<bool> {
        match self {
            PyJson::Bool(flag) => Some(*flag),
            _ => None,
        }
    }

    /// The value of a non-bool `int` that fits in `i64`.
    pub fn as_i64(&self) -> Option<i64> {
        match self {
            PyJson::Int(value) => Some(*value),
            _ => None,
        }
    }

    /// The dict of a dict value, or the value itself.
    ///
    /// # Errors
    /// Returns `self` unchanged when it is not a dict.
    pub fn into_dict(mut self) -> Result<PyDict, PyJson> {
        if let PyJson::Dict(map) = &mut self {
            return Ok(std::mem::take(map));
        }
        Err(self)
    }

    /// The items of a list value, or the value itself.
    ///
    /// # Errors
    /// Returns `self` unchanged when it is not a list.
    pub fn into_list(mut self) -> Result<Vec<PyJson>, PyJson> {
        if let PyJson::List(items) = &mut self {
            return Ok(std::mem::take(items));
        }
        Err(self)
    }

    /// The text of a string value, or the value itself.
    ///
    /// # Errors
    /// Returns `self` unchanged when it is not a string.
    pub fn into_string(mut self) -> Result<String, PyJson> {
        if let PyJson::Str(text) = &mut self {
            return Ok(std::mem::take(text));
        }
        Err(self)
    }

    fn is_container(&self) -> bool {
        matches!(self, PyJson::List(_) | PyJson::Dict(_))
    }

    /// `dict.get(key)` on a dict value; `None` for other types.
    pub fn get(&self, key: &str) -> Option<&PyJson> {
        self.as_dict().and_then(|map| map.get(key))
    }

    /// `json.dumps(value)` with CPython's defaults (the storage format of
    /// `check_result.data` and `collector_incident.payload`).
    ///
    /// # Errors
    /// Never fails for values produced by [`loads`]; see [`dumps`].
    pub fn to_json_string(&self) -> Result<String, DumpError> {
        dumps(self, &DumpOpts::python_default())
    }
}

impl PartialEq for PyJson {
    fn eq(&self, other: &Self) -> bool {
        let mut pending = vec![(self, other)];
        while let Some((a, b)) = pending.pop() {
            let same = match (a, b) {
                (PyJson::Null, PyJson::Null) => true,
                (PyJson::Bool(x), PyJson::Bool(y)) => x == y,
                (PyJson::Int(x), PyJson::Int(y)) => x == y,
                (PyJson::BigInt(x), PyJson::BigInt(y)) => x == y,
                (PyJson::Float(x), PyJson::Float(y)) => x.to_bits() == y.to_bits(),
                (PyJson::Str(x), PyJson::Str(y)) => x == y,
                (PyJson::List(x), PyJson::List(y)) => {
                    pending.extend(x.iter().zip(y.iter()));
                    x.len() == y.len()
                }
                (PyJson::Dict(x), PyJson::Dict(y)) => {
                    for ((xk, xv), (yk, yv)) in x.iter().zip(y.iter()) {
                        if xk != yk {
                            return false;
                        }
                        pending.push((xv, yv));
                    }
                    x.len() == y.len()
                }
                _ => false,
            };
            if !same {
                return false;
            }
        }
        true
    }
}

impl Clone for PyJson {
    fn clone(&self) -> Self {
        match self {
            PyJson::Null => PyJson::Null,
            PyJson::Bool(flag) => PyJson::Bool(*flag),
            PyJson::Int(number) => PyJson::Int(*number),
            PyJson::BigInt(number) => PyJson::BigInt(number.clone()),
            PyJson::Float(number) => PyJson::Float(*number),
            PyJson::Str(text) => PyJson::Str(text.clone()),
            PyJson::List(_) | PyJson::Dict(_) => clone_container(self),
        }
    }
}

/// A container being copied by [`clone_container`], with the key under which
/// it goes into its parent dict.
enum CloneFrame<'a> {
    List(std::slice::Iter<'a, PyJson>, Vec<PyJson>, Option<String>),
    Dict(
        indexmap::map::Iter<'a, String, PyJson>,
        PyDict,
        Option<String>,
    ),
}

impl<'a> CloneFrame<'a> {
    fn open(value: &'a PyJson, key: Option<String>) -> Self {
        match value {
            PyJson::Dict(map) => {
                CloneFrame::Dict(map.iter(), PyDict::with_capacity(map.len()), key)
            }
            PyJson::List(items) => {
                CloneFrame::List(items.iter(), Vec::with_capacity(items.len()), key)
            }
            _ => unreachable!("only containers are opened"),
        }
    }

    fn next_child(&mut self) -> Option<(Option<String>, &'a PyJson)> {
        match self {
            CloneFrame::List(source, _, _) => source.next().map(|child| (None, child)),
            CloneFrame::Dict(source, _, _) => source.next().map(|(k, v)| (Some(k.clone()), v)),
        }
    }

    fn attach(&mut self, key: Option<String>, value: PyJson) {
        match self {
            CloneFrame::List(_, items, _) => items.push(value),
            CloneFrame::Dict(_, map, _) => {
                map.insert(key.unwrap_or_default(), value);
            }
        }
    }

    fn close(self) -> (Option<String>, PyJson) {
        match self {
            CloneFrame::List(_, items, key) => (key, PyJson::List(items)),
            CloneFrame::Dict(_, map, key) => (key, PyJson::Dict(map)),
        }
    }
}

fn clone_container(root: &PyJson) -> PyJson {
    let mut stack = vec![CloneFrame::open(root, None)];
    loop {
        let Some(frame) = stack.last_mut() else {
            unreachable!("the root frame returns before the stack empties");
        };
        match frame.next_child() {
            Some((key, child)) if child.is_container() => {
                stack.push(CloneFrame::open(child, key));
            }
            Some((key, child)) => frame.attach(key, child.clone()),
            None => {
                let Some(done) = stack.pop() else {
                    unreachable!("frame checked above");
                };
                let (key, value) = done.close();
                match stack.last_mut() {
                    Some(parent) => parent.attach(key, value),
                    None => return value,
                }
            }
        }
    }
}

impl Drop for PyJson {
    /// Tear nested containers down iteratively instead of recursively.
    fn drop(&mut self) {
        let mut pending: Vec<PyJson> = match self {
            PyJson::List(items) if items.iter().any(PyJson::is_container) => std::mem::take(items),
            PyJson::Dict(map) if map.values().any(PyJson::is_container) => {
                map.drain(..).map(|(_, value)| value).collect()
            }
            _ => return,
        };
        while let Some(mut value) = pending.pop() {
            match &mut value {
                PyJson::List(items) => pending.append(items),
                PyJson::Dict(map) => pending.extend(map.drain(..).map(|(_, v)| v)),
                _ => {}
            }
            // `value` is dropped here with empty containers.
        }
    }
}

impl From<bool> for PyJson {
    fn from(value: bool) -> Self {
        PyJson::Bool(value)
    }
}

impl From<i64> for PyJson {
    fn from(value: i64) -> Self {
        PyJson::Int(value)
    }
}

impl From<f64> for PyJson {
    fn from(value: f64) -> Self {
        PyJson::Float(value)
    }
}

impl From<&str> for PyJson {
    fn from(value: &str) -> Self {
        PyJson::Str(value.to_owned())
    }
}

impl From<String> for PyJson {
    fn from(value: String) -> Self {
        PyJson::Str(value)
    }
}

impl From<Vec<PyJson>> for PyJson {
    fn from(value: Vec<PyJson>) -> Self {
        PyJson::List(value)
    }
}

impl From<PyDict> for PyJson {
    fn from(value: PyDict) -> Self {
        PyJson::Dict(value)
    }
}

#[cfg(test)]
mod tests;
