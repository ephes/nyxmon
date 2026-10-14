//! Row decoding (`sqlite_repo.py:row_to_check`, `_parse_check_data`,
//! `NotificationState.from_row`; PLAN.md 3.5).

use nyxmon_core::PyJson;
use nyxmon_core::model::{Check, NOTIFICATION_STATE_COLUMNS, NotificationState};
use nyxmon_core::pyjson::{loads, py_int};
use rusqlite::types::{Type, ValueRef};
use rusqlite::{Connection, OptionalExtension, Row};

use crate::error::StoreError;

/// The `health_check` columns every check query selects or returns, in
/// `row_to_check` order.
pub(crate) const CHECK_COLUMNS: &str = "id, service_id, name, check_type, url, check_interval, \
     next_check_time, processing_started_at, status, disabled, data";

fn conversion_error(index: usize, kind: Type, message: String) -> rusqlite::Error {
    rusqlite::Error::FromSqlConversionFailure(index, kind, message.into())
}

/// Python `int(value or 0)` on a column value.
pub(crate) fn int_or_zero(row: &Row<'_>, index: usize) -> rusqlite::Result<i64> {
    let value = row.get_ref(index)?;
    let parse_text = |bytes: &[u8], kind: Type| -> rusqlite::Result<i64> {
        if bytes.is_empty() {
            return Ok(0);
        }
        let text = std::str::from_utf8(bytes)
            .map_err(|err| conversion_error(index, kind, err.to_string()))?;
        py_int(text)
            .ok()
            .and_then(|big| i64::try_from(&big).ok())
            .ok_or_else(|| conversion_error(index, kind, format!("not an integer: {text:?}")))
    };
    match value {
        ValueRef::Null => Ok(0),
        ValueRef::Integer(number) => Ok(number),
        ValueRef::Real(number) => {
            // `int(float)` truncates; out-of-range values cannot be stored back.
            let whole = number.trunc();
            // `i64::MAX as f64` is 2**63, exclusive; `i64::MIN as f64` is exact.
            if whole >= i64::MIN as f64 && whole < i64::MAX as f64 {
                Ok(whole as i64)
            } else {
                Err(conversion_error(
                    index,
                    Type::Real,
                    format!("cannot convert {number} to an integer"),
                ))
            }
        }
        ValueRef::Text(bytes) => parse_text(bytes, Type::Text),
        ValueRef::Blob(bytes) => parse_text(bytes, Type::Blob),
    }
}

/// `str(value or "")` for text columns.
pub(crate) fn text_or_empty(row: &Row<'_>, index: usize) -> rusqlite::Result<String> {
    match row.get_ref(index)? {
        ValueRef::Null => Ok(String::new()),
        ValueRef::Integer(number) => Ok(number.to_string()),
        ValueRef::Real(number) => Ok(nyxmon_core::pyjson::py_float_repr(number)),
        ValueRef::Text(bytes) | ValueRef::Blob(bytes) => String::from_utf8(bytes.to_vec())
            .map_err(|err| conversion_error(index, Type::Text, err.to_string())),
    }
}

/// Python `bool(value)` for the `disabled` column (stored as 0/1).
pub(crate) fn truthy(row: &Row<'_>, index: usize) -> rusqlite::Result<bool> {
    Ok(match row.get_ref(index)? {
        ValueRef::Null => false,
        ValueRef::Integer(number) => number != 0,
        ValueRef::Real(number) => number != 0.0,
        ValueRef::Text(bytes) | ValueRef::Blob(bytes) => !bytes.is_empty(),
    })
}

/// `_parse_check_data`: decode the `health_check.data` column.
///
/// NULL or empty → `{}`. The JSON text `null` also reads as `{}` (PLAN.md
/// D-11). A value that cannot be decoded (a lone surrogate, D-8; an integer
/// beyond CPython's 4300-digit limit; text that is not UTF-8 or not JSON,
/// which Django's `JSON_VALID` check otherwise prevents) reads as
/// [`PyJson::Null`]; see [`data_is_undecodable`]. Python raises in
/// `row_to_check` instead, after the claim has committed, which strands the
/// whole claimed batch.
pub(crate) fn decode_check_data(value: ValueRef<'_>) -> PyJson {
    let decoded = match value {
        ValueRef::Null => return PyJson::dict(),
        ValueRef::Text(bytes) | ValueRef::Blob(bytes) => {
            if bytes.is_empty() {
                return PyJson::dict();
            }
            // Text and `bytes.decode("utf-8")` both go through `json.loads(str)`.
            match std::str::from_utf8(bytes) {
                Ok(text) => loads(text),
                Err(_) => return PyJson::Null,
            }
        }
        // `_parse_check_data` passes non-text values through unchanged.
        ValueRef::Integer(number) => return PyJson::Int(number),
        ValueRef::Real(number) => return PyJson::Float(number),
    };
    match decoded {
        Ok(PyJson::Null) => PyJson::dict(),
        Ok(data) => data,
        Err(_) => PyJson::Null,
    }
}

/// Whether a check's `data` column could not be decoded.
///
/// Valid `null` text reads as `{}` (D-11), so [`PyJson::Null`] in
/// [`Check::data`] (or `HeldCheck::data`) only ever marks an undecodable
/// column. The runner reports such a check as `configuration_error`
/// ("check data is not valid JSON", PLAN.md D-8); readers of the
/// notification policy, suppression and site dependency treat it as "no
/// configuration" like every other non-object.
pub fn data_is_undecodable(data: &PyJson) -> bool {
    data.is_null()
}

/// `row_to_check` over [`CHECK_COLUMNS`].
pub(crate) fn row_to_check(row: &Row<'_>) -> rusqlite::Result<Check> {
    let processing_started_at = int_or_zero(row, 7)?;
    Ok(Check {
        check_id: int_or_zero(row, 0)?,
        service_id: int_or_zero(row, 1)?,
        name: text_or_empty(row, 2)?,
        check_type: text_or_empty(row, 3)?,
        url: text_or_empty(row, 4)?,
        check_interval: int_or_zero(row, 5)?,
        next_check_time: int_or_zero(row, 6)?,
        processing_started_at,
        status: text_or_empty(row, 8)?,
        disabled: truthy(row, 9)?,
        data: decode_check_data(row.get_ref(10)?),
        // `Check.__init__`: the claim identity is the loaded lease timestamp.
        claim_started_at: processing_started_at,
    })
}

/// `_select_notification_state`: missing row or NULLs read as zeros.
pub(crate) fn select_notification_state(
    connection: &Connection,
    check_id: i64,
) -> Result<NotificationState, StoreError> {
    let sql = format!(
        "SELECT {} FROM check_notification_state WHERE check_id = ?",
        NOTIFICATION_STATE_COLUMNS.join(", ")
    );
    let row = connection
        .query_row(&sql, [check_id], |row| {
            (0..NOTIFICATION_STATE_COLUMNS.len())
                .map(|index| int_or_zero(row, index).map(Some))
                .collect::<rusqlite::Result<Vec<Option<i64>>>>()
        })
        .optional()?;
    Ok(NotificationState::from_row(row.as_deref()))
}
