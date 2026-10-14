//! Repository-owned delivery markers of collector incident payloads
//! (`adapters/repositories/interface.py`).
//!
//! `delivery_pending`/`delivery_attempt` record that an alert was claimed
//! but not yet acknowledged. The store writes them; callers that rebuild a
//! payload must carry them over.

use crate::pyjson::{PyDict, PyJson};

/// `DELIVERY_PENDING_KEY`.
pub const DELIVERY_PENDING_KEY: &str = "delivery_pending";
/// `DELIVERY_ATTEMPT_KEY`.
pub const DELIVERY_ATTEMPT_KEY: &str = "delivery_attempt";
/// `DELIVERY_MARKER_KEYS`.
pub const DELIVERY_MARKER_KEYS: [&str; 2] = [DELIVERY_PENDING_KEY, DELIVERY_ATTEMPT_KEY];

/// `carry_delivery_markers`: `payload` with the markers of `existing`
/// restored (a key already in `payload` keeps its position).
pub fn carry_delivery_markers(payload: &PyDict, existing: &PyDict) -> PyDict {
    let mut merged = payload.clone();
    for key in DELIVERY_MARKER_KEYS {
        if let Some(value) = existing.get(key) {
            merged.insert(key.to_owned(), value.clone());
        }
    }
    merged
}

/// `with_delivery_intent`: `payload` marked "alert `attempt` claimed, not
/// yet sent".
pub fn with_delivery_intent(payload: &PyDict, attempt: i64) -> PyDict {
    let mut merged = payload.clone();
    merged.insert(DELIVERY_PENDING_KEY.to_owned(), PyJson::Bool(true));
    merged.insert(DELIVERY_ATTEMPT_KEY.to_owned(), PyJson::Int(attempt));
    merged
}

/// `without_delivery_markers`: the inverse of [`with_delivery_intent`];
/// every other key keeps its position (`dict.pop`).
pub fn without_delivery_markers(payload: &PyDict) -> PyDict {
    let mut merged = payload.clone();
    for key in DELIVERY_MARKER_KEYS {
        merged.shift_remove(key);
    }
    merged
}
