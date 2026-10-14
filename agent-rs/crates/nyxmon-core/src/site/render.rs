//! Value coercion and message rendering of the observer
//! (`site_connectivity.py:_as_int`, `_iso`, `_duration`,
//! `_render_summary_line`, `_summary_line`, `_record_is_due`), PLAN.md
//! sections 6.5-6.7.

use std::fmt;

use crate::pyjson::{PyDict, PyJson, char_len, py_truthy};

/// The Python exception a coercion or rendering step raised. Python lets it
/// escape the round (`run_once`), so the Rust observer propagates it the
/// same way with every mutation done before it kept.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Raised(pub &'static str);

impl fmt::Display for Raised {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.0)
    }
}

/// `SITE_DELIVERY_RETRY_SECONDS`: retry cadence of an undelivered message.
pub const SITE_DELIVERY_RETRY_SECONDS: i64 = 60;

/// `SITE_SUMMARY_MAX_RECORDS`.
pub const SITE_SUMMARY_MAX_RECORDS: usize = 5;

/// `SITE_SUMMARY_MAX_CHARS`: Telegram rejects bodies above 4096 characters.
pub const SITE_SUMMARY_MAX_CHARS: usize = 3500;

/// `_as_int`: bools and non-numbers are 0, floats are truncated.
///
/// `int(inf)` raises `OverflowError` and `int(nan)` `ValueError` in Python;
/// both are reproduced. Integers beyond 64 bits (Python keeps them exact)
/// saturate: the payload is written only by the agent itself, with 64-bit
/// values, so they cannot occur in a row this code reads back.
pub fn as_int(value: Option<&PyJson>) -> Result<i64, Raised> {
    match value {
        Some(PyJson::Int(number)) => Ok(*number),
        Some(PyJson::BigInt(number)) => Ok(crate::pyjson::bigint_to_i64_saturating(number)),
        Some(PyJson::Float(number)) if number.is_nan() => Err(Raised("ValueError")),
        Some(PyJson::Float(number)) if number.is_infinite() => Err(Raised("OverflowError")),
        // `as` truncates toward zero and saturates beyond the i64 range.
        Some(PyJson::Float(number)) => Ok(*number as i64),
        _ => Ok(0),
    }
}

/// `_as_mapping(value).get(key)`: a non-object reads as `{}`.
pub fn mapping_get<'a>(value: Option<&'a PyJson>, key: &str) -> Option<&'a PyJson> {
    match value {
        Some(PyJson::Dict(map)) => map.get(key),
        _ => None,
    }
}

/// `_iso`: `%Y-%m-%dT%H:%M:%SZ` in UTC, `unknown` for epochs `<= 0`.
///
/// `time.gmtime` fails (`OSError`, `EOVERFLOW`) once the year no longer fits
/// the C `struct tm`; the same bound is applied here.
pub fn iso(epoch: i64) -> Result<String, Raised> {
    if epoch <= 0 {
        return Ok("unknown".to_owned());
    }
    let days = epoch.div_euclid(86_400);
    let seconds = epoch.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    if year - 1900 > i64::from(i32::MAX) {
        return Err(Raised("OSError"));
    }
    Ok(format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        seconds / 3600,
        seconds % 3600 / 60,
        seconds % 60
    ))
}

/// Proleptic Gregorian date of a day count since 1970-01-01 (H. Hinnant's
/// `civil_from_days`), for non-negative day counts.
fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    (year, month, day)
}

/// `_duration`: `Ns`, `MmSSs` or `HhMMm`.
pub fn duration(seconds: i64) -> String {
    if seconds < 60 {
        return format!("{}s", seconds.max(0));
    }
    let (minutes, remainder) = (seconds / 60, seconds % 60);
    if minutes < 60 {
        return format!("{minutes}m{remainder:02}s");
    }
    format!("{}h{:02}m", minutes / 60, minutes % 60)
}

/// `_render_summary_line(record, names, omitted)`.
pub fn render_summary_line(
    record: &PyDict,
    names: &[&str],
    omitted: usize,
) -> Result<String, Raised> {
    let raw_paths = record.get("paths");
    let mut parts = Vec::with_capacity(names.len() + 1);
    for name in names {
        let entry = mapping_get(raw_paths, name);
        let down = iso(as_int(mapping_get(entry, "down_since"))?)?;
        let recovered = iso(as_int(mapping_get(entry, "recovered_at"))?)?;
        parts.push(format!("{name} down {down} until {recovered}"));
    }
    if omitted > 0 {
        parts.push(format!("+{omitted} more path(s)"));
    }
    let path_text = parts.join("; ");
    let ended = as_int(record.get("ended_at"))?;
    let started = as_int(record.get("started_at"))?;
    let lasted = ended.saturating_sub(started).max(0);
    let held = as_int(record.get("held_at_close"))?;
    let incident_id = as_int(record.get("incident_id"))?;
    let paths = if path_text.is_empty() {
        String::new()
    } else {
        format!(" ({path_text})")
    };
    let delivered = if record.get("ongoing_delivered").is_some_and(py_truthy) {
        "yes"
    } else {
        "no"
    };
    Ok(format!(
        "Site connectivity recovered: outage {incident_id} lasted {}{paths}. Ongoing alert \
         delivered: {delivered}. {held} dependent check(s) were held at close and are being \
         rechecked.",
        duration(lasted)
    ))
}

/// `_summary_line(record, limit=...)`: drop paths from the end of the sorted
/// list until the line fits, hard-truncate as a last resort.
pub fn summary_line(record: &PyDict, limit: usize) -> Result<String, Raised> {
    let mut names: Vec<&str> = match record.get("paths") {
        Some(PyJson::Dict(paths)) => paths.keys().map(String::as_str).collect(),
        _ => Vec::new(),
    };
    // Python sorts by code point; UTF-8 byte order is the same order.
    names.sort_unstable();
    let mut kept = names.len();
    let mut line = render_summary_line(record, &names, 0)?;
    while char_len(&line) > limit && kept > 0 {
        kept -= 1;
        line = render_summary_line(record, &names[..kept], names.len() - kept)?;
    }
    if char_len(&line) > limit {
        line = line.chars().take(limit).collect();
    }
    Ok(line)
}

/// `_record_is_due`: never attempted, or attempted at least
/// [`SITE_DELIVERY_RETRY_SECONDS`] ago.
pub fn record_is_due(record: &PyDict, now: i64) -> Result<bool, Raised> {
    let attempt_at = as_int(record.get("attempt_at"))?;
    Ok(attempt_at <= 0 || now.saturating_sub(attempt_at) >= SITE_DELIVERY_RETRY_SECONDS)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso_and_duration_match_python() {
        assert_eq!(iso(0).unwrap(), "unknown");
        assert_eq!(iso(-5).unwrap(), "unknown");
        assert_eq!(iso(1).unwrap(), "1970-01-01T00:00:01Z");
        assert_eq!(iso(1_700_000_000).unwrap(), "2023-11-14T22:13:20Z");
        assert_eq!(iso(951_782_400).unwrap(), "2000-02-29T00:00:00Z");
        assert_eq!(iso(253_402_300_800).unwrap(), "10000-01-01T00:00:00Z");
        assert_eq!(iso(i64::MAX), Err(Raised("OSError")));
        assert_eq!(duration(-3), "0s");
        assert_eq!(duration(59), "59s");
        assert_eq!(duration(60), "1m00s");
        assert_eq!(duration(3599), "59m59s");
        assert_eq!(duration(3600), "1h00m");
        assert_eq!(duration(10_800), "3h00m");
    }

    #[test]
    fn as_int_matches_python() {
        assert_eq!(as_int(None), Ok(0));
        assert_eq!(as_int(Some(&PyJson::Bool(true))), Ok(0));
        assert_eq!(as_int(Some(&PyJson::Float(-2.9))), Ok(-2));
        assert_eq!(as_int(Some(&PyJson::from("5"))), Ok(0));
        assert_eq!(
            as_int(Some(&PyJson::Float(f64::NAN))),
            Err(Raised("ValueError"))
        );
        assert_eq!(
            as_int(Some(&PyJson::Float(f64::NEG_INFINITY))),
            Err(Raised("OverflowError"))
        );
    }
}
