//! IMAP check configuration and the pure helpers of the IMAP executor
//! (`domain/imap_config.py`, `imap_executor.py:_normalize_host`, the UID
//! match and INTERNALDATE conversion of `ImapLibSession._fetch_internaldate`,
//! `imaplib.Internaldate2tuple`).

use crate::config::mail::{IntValue, exact_int, get, mail_duration};
use crate::config::smtp::{civil_from_days, iso_utc_z};
use crate::config::{ConfigError, PyException, sorted_set_repr, str_set_contains};
use crate::pyjson::{PyJson, py_isspace, py_truthy, py_type_name};
use crate::pyurl::urlsplit_hostname;

/// `ImapCheckConfig.tls_mode`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ImapTls {
    Implicit,
    Starttls,
    None,
}

/// A validated `ImapCheckConfig`.
///
/// `host`, `username`, `password`, `password_secret`, `search_subject` and
/// `folder` are not type-checked by Python and keep their raw JSON values;
/// the executor reproduces what `imaplib` does with them.
#[derive(Clone, Debug, PartialEq)]
pub struct ImapConfig {
    pub host: PyJson,
    pub username: PyJson,
    pub password: PyJson,
    pub password_secret: PyJson,
    pub search_subject: PyJson,
    pub folder: PyJson,
    pub port: u16,
    pub tls_mode: ImapTls,
    pub max_age_minutes: i64,
    pub delete_after_check: bool,
    pub timeout: f64,
    pub retries: u32,
    pub retry_delay: f64,
    /// `no_recent_message_severity == "warning"` (else `"critical"`).
    pub warning_severity: bool,
    pub verify: bool,
}

const TLS_MODES: [&str; 3] = ["implicit", "starttls", "none"];
const SEVERITIES: [&str; 2] = ["critical", "warning"];

/// The `configuration_error` for a truthy `host` that is a list or an
/// object: Python accepts it and fails later in ways that depend on its
/// contents (PLAN.md 1.5 D-18).
pub const UNSUPPORTED_HOST: &str = "host is not supported by the Rust agent: not a string";

impl ImapConfig {
    /// `ImapCheckConfig.from_dict(data)` (which calls `validate()`), as the
    /// executor runs it.
    ///
    /// # Errors
    /// [`ConfigError::Invalid`] for the `ValueError`s (`configuration_error`),
    /// [`ConfigError::Exception`] for exceptions that escape the executor
    /// (`AttributeError` for non-dict data, `TypeError` for unhashable
    /// `tls_mode`/`no_recent_message_severity`).
    pub fn from_data(data: &PyJson) -> Result<ImapConfig, ConfigError> {
        if !matches!(data, PyJson::Dict(_)) {
            // `data.get(...)` on a non-dict (`None` is mapped to `{}` by the
            // caller, PLAN.md D-11).
            return Err(PyException::ATTRIBUTE_ERROR.into());
        }
        let field = |key: &str| get(data, key);
        let or = |key: &str, default: &str| {
            field(key)
                .cloned()
                .unwrap_or_else(|| PyJson::Str(default.to_owned()))
        };
        let host = or("host", "");
        let username = or("username", "");
        let password = field("password").cloned().unwrap_or(PyJson::Null);
        let password_secret = field("password_secret").cloned().unwrap_or(PyJson::Null);
        let search_subject = or("search_subject", "");
        let tls_mode = or("tls_mode", "implicit");
        let severity = or("no_recent_message_severity", "critical");
        if !py_truthy(&host) {
            return Err(ConfigError::invalid("host is required"));
        }
        if !py_truthy(&username) {
            return Err(ConfigError::invalid("username is required"));
        }
        if !(py_truthy(&password) || py_truthy(&password_secret)) {
            return Err(ConfigError::invalid(
                "password or password_secret is required",
            ));
        }
        if !py_truthy(&search_subject) {
            return Err(ConfigError::invalid("search_subject is required"));
        }
        if !str_set_contains(&tls_mode, &TLS_MODES)? {
            return Err(ConfigError::invalid(format!(
                "tls_mode must be one of {}",
                sorted_set_repr(&TLS_MODES)
            )));
        }
        if !str_set_contains(&severity, &SEVERITIES)? {
            return Err(ConfigError::invalid(format!(
                "no_recent_message_severity must be one of {}",
                sorted_set_repr(&SEVERITIES)
            )));
        }
        let verify = match field("verify") {
            None | Some(PyJson::Null) => true,
            Some(PyJson::Bool(flag)) => *flag,
            Some(_) => return Err(ConfigError::invalid("verify must be a boolean")),
        };
        // validate()
        let delete_after_check = match field("delete_after_check") {
            None => true,
            Some(PyJson::Bool(flag)) => *flag,
            Some(_) => {
                return Err(ConfigError::invalid("delete_after_check must be a boolean"));
            }
        };
        let mut ints = [0i64; 3];
        for (slot, (name, default, low, high)) in ints.iter_mut().zip([
            ("port", 993, 1, 65535),
            ("retries", 2, 0, 100),
            ("max_age_minutes", 30, 1, 525_600),
        ]) {
            match field(name).map_or(Some(IntValue::Small(default)), exact_int) {
                Some(IntValue::Small(n)) if (low..=high).contains(&n) => *slot = n,
                _ => {
                    return Err(ConfigError::invalid(format!(
                        "{name} must be an integer between {low} and {high}"
                    )));
                }
            }
        }
        let timeout = mail_duration("timeout", field("timeout").unwrap_or(&PyJson::Float(30.0)))
            .map_err(ConfigError::Invalid)?;
        let retry_delay = mail_duration(
            "retry_delay",
            field("retry_delay").unwrap_or(&PyJson::Float(10.0)),
        )
        .map_err(ConfigError::Invalid)?;
        if matches!(host, PyJson::List(_) | PyJson::Dict(_)) {
            return Err(ConfigError::invalid(UNSUPPORTED_HOST));
        }
        let tls_mode = match tls_mode.as_str() {
            Some("starttls") => ImapTls::Starttls,
            Some("none") => ImapTls::None,
            _ => ImapTls::Implicit,
        };
        Ok(ImapConfig {
            host,
            username,
            password,
            password_secret,
            search_subject,
            folder: or("folder", "INBOX"),
            port: ints[0] as u16,
            tls_mode,
            max_age_minutes: ints[2],
            delete_after_check,
            timeout,
            retries: ints[1] as u32,
            retry_delay,
            warning_severity: severity.as_str() == Some("warning"),
            verify,
        })
    }

    /// `no_recent_message_severity`.
    pub fn severity(&self) -> &'static str {
        if self.warning_severity {
            "warning"
        } else {
            "critical"
        }
    }

    /// The host the session connects to: `_normalize_host(config.host)`.
    ///
    /// # Errors
    /// The text of the exception Python raises (`urlparse`'s `ValueError`,
    /// or the `TypeError` of `"://" in host` for a number or boolean).
    pub fn session_host(&self) -> Result<String, String> {
        match &self.host {
            PyJson::Str(text) => normalize_host(text),
            other => Err(format!(
                "argument of type '{}' is not iterable",
                py_type_name(other)
            )),
        }
    }
}

/// `ImapCheckExecutor._normalize_host` for a `str` host: the
/// `urlparse(host).hostname` of a URL (when it has one), else the host.
///
/// # Errors
/// The `ValueError` text `urlsplit` raises for malformed brackets.
pub fn normalize_host(host: &str) -> Result<String, String> {
    if !host.contains("://") {
        return Ok(host.to_owned());
    }
    let hostname = urlsplit_hostname(host).map_err(|err| err.0)?;
    Ok(hostname.unwrap_or_else(|| host.to_owned()))
}

/// The UID of the first `\bUID ([0-9]+)\b` (ASCII case-insensitive) in a
/// FETCH response line.
pub fn fetch_uid(line: &[u8]) -> Option<&[u8]> {
    let word = |b: u8| b.is_ascii_alphanumeric() || b == b'_';
    (0..line.len().saturating_sub(4)).find_map(|i| {
        if !line[i..i + 4].eq_ignore_ascii_case(b"UID ") || (i > 0 && word(line[i - 1])) {
            return None;
        }
        let digits = line[i + 4..]
            .iter()
            .take_while(|b| b.is_ascii_digit())
            .count();
        let end = i + 4 + digits;
        (digits > 0 && !line.get(end).is_some_and(|&b| word(b))).then(|| &line[i + 4..end])
    })
}

/// `search_data[0].decode().split()`: the UIDs of a `SEARCH` response.
///
/// # Errors
/// The `UnicodeDecodeError` text for data that is not UTF-8.
pub fn search_ids(data: &[u8]) -> Result<Vec<String>, String> {
    match std::str::from_utf8(data) {
        Ok(text) => Ok(text
            .split(py_isspace)
            .filter(|id| !id.is_empty())
            .map(str::to_owned)
            .collect()),
        Err(_) => Err(utf8_decode_error(data).unwrap_or_default()),
    }
}

/// What `_fetch_internaldate` makes of the matching FETCH response line.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum InternalDate {
    /// Seconds since the epoch (UTC).
    At(i64),
    /// No `INTERNALDATE "..."` in imaplib's format: the message is skipped.
    Missing,
    /// `Internaldate2tuple` raised, and so did the `parsedate_to_datetime`
    /// fallback: the text of the escaping exception (`unexpected_error`).
    Error(String),
}

const MONTHS: [&[u8]; 12] = [
    b"Jan", b"Feb", b"Mar", b"Apr", b"May", b"Jun", b"Jul", b"Aug", b"Sep", b"Oct", b"Nov", b"Dec",
];

/// `imaplib.Internaldate2tuple` and the executor's conversion to an aware
/// UTC `datetime` (`fromtimestamp(mktime(localtime(utc)))` is `utc`).
///
/// When `Internaldate2tuple` raises (a month name that is not English, year
/// 0), Python falls back to `parsedate_to_datetime` on the whole line, which
/// raises for every FETCH response line; its message is reproduced. Dates
/// whose UTC year leaves 1..=9999 report `year <y> is out of range`, as
/// `datetime.fromtimestamp` does on glibc (macOS `mktime` already fails
/// before 1900; PLAN.md 1.5 D-18).
pub fn parse_internaldate(line: &[u8]) -> InternalDate {
    const KEY: &[u8] = b"INTERNALDATE \"";
    // `re.match(r'.*INTERNALDATE "<date>"')`: `.` stops at the first
    // newline, and the greedy `.*` prefers the last matching occurrence.
    let scope = &line[..line.iter().position(|&b| b == b'\n').unwrap_or(line.len())];
    let fields = (0..scope.len().saturating_sub(KEY.len() - 1))
        .rev()
        .filter(|&i| scope[i..].starts_with(KEY))
        .find_map(|i| date_fields(&scope[i + KEY.len()..]));
    let Some(f) = fields else {
        return InternalDate::Missing;
    };
    let fallback = || {
        InternalDate::Error(match std::str::from_utf8(line) {
            Ok(text) => format!("Invalid date value or format \"{text}\""),
            Err(_) => utf8_decode_error(line).unwrap_or_default(),
        })
    };
    let Some(month) = MONTHS.iter().position(|m| *m == f.month) else {
        return fallback();
    };
    if f.year == 0 {
        // `calendar.timegm` -> `datetime.date(0, ...)` raises.
        return fallback();
    }
    let days = days_from_civil(f.year, month as i64 + 1) + f.day - 1;
    let utc = days * 86_400 + f.hour * 3600 + f.min * 60 + f.sec - f.zone;
    // `datetime.fromtimestamp` covers years 1..=9999 only.
    if !(-62_135_596_800..=253_402_300_799).contains(&utc) {
        let (year, _, _) = civil_from_days(utc.div_euclid(86_400));
        return InternalDate::Error(format!("year {year} is out of range"));
    }
    InternalDate::At(utc)
}

struct DateFields<'a> {
    day: i64,
    month: &'a [u8],
    year: i64,
    hour: i64,
    min: i64,
    sec: i64,
    /// Offset east of UTC in seconds.
    zone: i64,
}

/// `(?P<day>[ 0123][0-9])-(?P<mon>[A-Z][a-z][a-z])-(?P<year>[0-9]{4})
/// (?P<hour>..):(?P<min>..):(?P<sec>..) (?P<zonen>[-+])(?P<zoneh>..)(?P<zonem>..)"`
fn date_fields(s: &[u8]) -> Option<DateFields<'_>> {
    if s.len() < 27 || s[26] != b'"' {
        return None;
    }
    let digit = |i: usize| s[i].is_ascii_digit();
    let num = |a: usize, b: usize| -> i64 {
        s[a..b]
            .iter()
            .filter(|d| d.is_ascii_digit())
            .fold(0, |n, d| n * 10 + i64::from(d - b'0'))
    };
    let ok = b" 0123".contains(&s[0])
        && digit(1)
        && s[2] == b'-'
        && s[3].is_ascii_uppercase()
        && s[4].is_ascii_lowercase()
        && s[5].is_ascii_lowercase()
        && s[6] == b'-'
        && (7..11).all(digit)
        && s[11] == b' '
        && digit(12)
        && digit(13)
        && s[14] == b':'
        && digit(15)
        && digit(16)
        && s[17] == b':'
        && digit(18)
        && digit(19)
        && s[20] == b' '
        && (s[21] == b'+' || s[21] == b'-')
        && (22..26).all(digit);
    if !ok {
        return None;
    }
    let zone = (num(22, 24) * 60 + num(24, 26)) * 60;
    Some(DateFields {
        day: num(0, 2),
        month: &s[3..6],
        year: num(7, 11),
        hour: num(12, 14),
        min: num(15, 17),
        sec: num(18, 20),
        zone: if s[21] == b'-' { -zone } else { zone },
    })
}

/// Days since 1970-01-01 of the first day of a proleptic Gregorian month.
fn days_from_civil(year: i64, month: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y.rem_euclid(400);
    let mp = (month + 9) % 12;
    let doy = (153 * mp + 2) / 5;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// `str(exc)` of the `UnicodeDecodeError` that `data.decode()` (strict
/// UTF-8) raises, or `None` when `data` is valid UTF-8.
pub fn utf8_decode_error(data: &[u8]) -> Option<String> {
    let err = std::str::from_utf8(data).err()?;
    let start = err.valid_up_to();
    let first = data[start];
    let prefix = "'utf-8' codec can't decode";
    Some(match err.error_len() {
        None if data.len() - start > 1 => format!(
            "{prefix} bytes in position {start}-{}: unexpected end of data",
            data.len() - 1
        ),
        None => format!("{prefix} byte 0x{first:02x} in position {start}: unexpected end of data"),
        Some(_) => {
            let reason = if (0x80..=0xc1).contains(&first) || first >= 0xf5 {
                "invalid start byte"
            } else {
                "invalid continuation byte"
            };
            format!("{prefix} byte 0x{first:02x} in position {start}: {reason}")
        }
    })
}

/// `datetime.isoformat()` of an aware UTC datetime with whole seconds.
pub fn iso_utc(unix: i64) -> String {
    let mut text = iso_utc_z(unix);
    text.pop();
    text.push_str("+00:00");
    text
}

#[cfg(test)]
mod tests;
