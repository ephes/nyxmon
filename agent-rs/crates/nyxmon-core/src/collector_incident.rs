//! Collector incident payloads and messages (`adapters/collector.py`,
//! PLAN.md section 5.8 steps 4 and 5).
//!
//! Pure helpers of the collector's two incidents: the merged payload of the
//! stale-lease incident and the message bodies of both incidents (the
//! synthetic check/result pair a collector alert is sent as is
//! `nyxmon_agent::incident_notification`). The payloads
//! are read back from the database, so the helpers follow Python's
//! `int()`, `str()` and `list()` semantics for whatever a row holds;
//! the exceptions Python does not catch are returned as `Err`.

use num_bigint::BigInt;

use crate::model::CollectorIncident;
use crate::pyjson::{PyDict, PyJson, PyNumError, py_int_of, py_str};

/// `COLLECTOR_STALE_BATCH_INCIDENT_KEY`.
pub const STALE_BATCH_INCIDENT_KEY: &str = "collector:stale_processing_lease";
/// `COLLECTOR_PAUSED_INCIDENT_KEY`.
pub const PAUSED_INCIDENT_KEY: &str = "collector:execution_paused";
/// `INCIDENT_CHECK_SAMPLE_LIMIT`.
pub const INCIDENT_CHECK_SAMPLE_LIMIT: usize = 20;
/// `INCIDENT_MESSAGE_ID_LIMIT`.
pub const INCIDENT_MESSAGE_ID_LIMIT: usize = 10;

/// `py_int_of` with the exceptions the collector catches (`TypeError`,
/// `ValueError`) as `None`; an `OverflowError` escapes.
fn int_or_caught(value: &PyJson) -> Result<Option<BigInt>, PyNumError> {
    match py_int_of(value) {
        Ok(number) => Ok(Some(number)),
        Err(PyNumError::OverflowError(message)) => Err(PyNumError::OverflowError(message)),
        Err(_) => Ok(None),
    }
}

/// `list(value)` of a decoded JSON value: the items of a list, the
/// characters of a string, the keys of a dict.
///
/// # Errors
/// `TypeError` for the scalar types, which are not iterable.
fn py_list(value: &PyJson) -> Result<Vec<PyJson>, PyNumError> {
    let not_iterable = |name: &str| {
        Err(PyNumError::TypeError(format!(
            "'{name}' object is not iterable"
        )))
    };
    match value {
        PyJson::List(items) => Ok(items.clone()),
        PyJson::Str(text) => Ok(text.chars().map(|c| PyJson::Str(c.to_string())).collect()),
        PyJson::Dict(map) => Ok(map.keys().map(|key| PyJson::Str(key.clone())).collect()),
        PyJson::Null => not_iterable("NoneType"),
        PyJson::Bool(_) => not_iterable("bool"),
        PyJson::Int(_) | PyJson::BigInt(_) => not_iterable("int"),
        PyJson::Float(_) => not_iterable("float"),
    }
}

/// `isinstance(value, int) and not isinstance(value, bool)`.
fn is_plain_int(value: &PyJson) -> bool {
    matches!(value, PyJson::Int(_) | PyJson::BigInt(_))
}

/// `isinstance(value, int)` as an integer (`bool` included).
fn as_int(value: &PyJson) -> Option<BigInt> {
    match value {
        PyJson::Bool(flag) => Some(BigInt::from(u8::from(*flag))),
        PyJson::Int(number) => Some(BigInt::from(*number)),
        PyJson::BigInt(number) => Some(number.clone()),
        _ => None,
    }
}

/// `_merge_stale_batch_payload(existing, recovered, now)` with the check
/// ids of `recovered`.
///
/// # Errors
/// What Python does not catch: an `OverflowError` of `int()` and the
/// `TypeError` of a `check_ids` value that is not iterable.
pub fn merge_stale_batch_payload(
    existing: Option<&CollectorIncident>,
    recovered: &[i64],
    now: i64,
) -> Result<PyDict, PyNumError> {
    let empty = PyDict::new();
    let previous = existing.map_or(&empty, |incident| &incident.payload);
    let zero = PyJson::Int(0);
    let reclaimed = int_or_caught(previous.get("reclaimed_count").unwrap_or(&zero))?;
    let iterations = match reclaimed {
        Some(_) => int_or_caught(previous.get("iteration_count").unwrap_or(&zero))?,
        None => None,
    };
    let (reclaimed, iterations) = match (reclaimed, iterations) {
        (Some(reclaimed), Some(iterations)) => (reclaimed, iterations),
        _ => (BigInt::from(0), BigInt::from(0)),
    };
    let mut sample: Vec<PyJson> = match previous.get("check_ids") {
        Some(value) => py_list(value)?.into_iter().filter(is_plain_int).collect(),
        None => Vec::new(),
    };
    for check_id in recovered {
        let id = PyJson::Int(*check_id);
        if !sample.iter().any(|known| as_int(known) == as_int(&id))
            && sample.len() < INCIDENT_CHECK_SAMPLE_LIMIT
        {
            sample.push(id);
        }
    }
    let mut payload = PyDict::new();
    payload.insert(
        "incident_type".to_owned(),
        PyJson::from("stale_processing_lease"),
    );
    payload.insert(
        "reclaimed_count".to_owned(),
        PyJson::from_bigint(reclaimed + recovered.len()),
    );
    payload.insert(
        "iteration_count".to_owned(),
        PyJson::from_bigint(iterations + 1),
    );
    payload.insert("check_ids".to_owned(), PyJson::List(sample));
    payload.insert("last_seen_at".to_owned(), PyJson::Int(now));
    Ok(payload)
}

/// `list(payload.get("check_ids", []))[:INCIDENT_MESSAGE_ID_LIMIT]`.
fn message_sample(payload: &PyDict) -> Result<Vec<PyJson>, PyNumError> {
    let mut sample = match payload.get("check_ids") {
        Some(value) => py_list(value)?,
        None => Vec::new(),
    };
    sample.truncate(INCIDENT_MESSAGE_ID_LIMIT);
    Ok(sample)
}

fn joined(sample: &[PyJson]) -> String {
    sample.iter().map(py_str).collect::<Vec<_>>().join(", ")
}

/// `_stale_batch_message(incident)`.
///
/// # Errors
/// A `check_ids` value that is not iterable (`TypeError`) and an
/// `OverflowError` of `int(last_seen_at)`.
pub fn stale_batch_message(incident: &CollectorIncident) -> Result<String, PyNumError> {
    let payload = &incident.payload;
    let zero = PyJson::Int(0);
    let count = payload.get("reclaimed_count").unwrap_or(&zero);
    let iterations = payload.get("iteration_count").unwrap_or(&zero);
    let sample = message_sample(payload)?;
    let opened_at = PyJson::Int(incident.opened_at);
    let last_seen = payload.get("last_seen_at").unwrap_or(&opened_at);
    let window = match int_or_caught(last_seen)? {
        Some(last_seen) => (last_seen - incident.opened_at).max(BigInt::from(0)),
        None => BigInt::from(0),
    };
    let mut listed = joined(&sample);
    if listed.is_empty() {
        listed = "none recorded".to_owned();
    }
    if let Some(count) = as_int(count)
        && count > BigInt::from(sample.len())
    {
        listed.push_str(&format!(" (+{} more)", count - sample.len()));
    }
    Ok(format!(
        "{} check processing lease(s) expired and were reclaimed over {window}s across {} \
         collector iteration(s). Affected check ids: {listed}. Every affected check was \
         released and rescheduled. This is one batch-level incident: lease recovery never \
         pages per check.",
        py_str(count),
        py_str(iterations),
    ))
}

/// `_paused_batch_message(incident)` at `now`.
///
/// # Errors
/// A `check_ids` value that is not iterable (`TypeError`).
pub fn paused_batch_message(incident: &CollectorIncident, now: i64) -> Result<String, PyNumError> {
    let payload = &incident.payload;
    let zero = PyJson::Int(0);
    let size = payload.get("batch_size").unwrap_or(&zero);
    let sample = message_sample(payload)?;
    let elapsed = (i128::from(now) - i128::from(incident.opened_at)).max(0);
    let listed = joined(&sample);
    let mut message = format!(
        "All new check executions are paused because an abandoned batch thread is still \
         running after {elapsed}s. The wedged batch holds {} check(s)",
        py_str(size)
    );
    if !listed.is_empty() {
        message.push_str(&format!(" (check ids: {listed})"));
    }
    message.push_str(
        ". Expired processing leases are still being reclaimed and rescheduled while \
         execution stays paused.",
    );
    Ok(message)
}

/// `_sync_paused_batch_incident`'s payload for an abandoned batch.
pub fn paused_batch_payload(check_ids: &[i64]) -> PyDict {
    let mut payload = PyDict::new();
    payload.insert(
        "incident_type".to_owned(),
        PyJson::from("collector_execution_paused"),
    );
    payload.insert(
        "batch_size".to_owned(),
        PyJson::Int(i64::try_from(check_ids.len()).unwrap_or(i64::MAX)),
    );
    payload.insert(
        "check_ids".to_owned(),
        PyJson::List(
            check_ids
                .iter()
                .take(INCIDENT_CHECK_SAMPLE_LIMIT)
                .map(|id| PyJson::Int(*id))
                .collect(),
        ),
    );
    payload
}

#[cfg(test)]
mod tests;
