//! SMTP check configuration and test message (`domain/smtp_config.py`,
//! `smtp_executor.py:_build_subject`, `_build_message`,
//! `_message_id_domain`).

use crate::config::mail::{
    IntValue, exact_int, get, mail_duration, non_dict_lookup_error, py_splitlines_count,
};
use crate::pyjson::{PyJson, py_str, py_strip, py_truthy, py_type_name};

/// `SmtpCheckConfig.tls`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SmtpTls {
    None,
    Starttls,
    Implicit,
}

/// A validated `SmtpCheckConfig`.
///
/// `host`, `username`, `password` and `password_secret` are not validated by
/// Python and keep their raw JSON values; the executor reproduces what
/// `smtplib` does with non-string values.
#[derive(Clone, Debug, PartialEq)]
pub struct SmtpConfig {
    pub host: PyJson,
    pub port: u16,
    pub tls: SmtpTls,
    pub username: PyJson,
    pub password: PyJson,
    pub password_secret: PyJson,
    pub from_addr: String,
    pub to_addr: String,
    pub subject_prefix: String,
    pub timeout: f64,
    /// `retries`; values beyond `u64` saturate (Python would loop as long).
    pub retries: u64,
    pub retry_delay: f64,
    pub verify: bool,
}

impl SmtpConfig {
    /// `SmtpCheckConfig.from_dict(data)` followed by `validate()`; the error
    /// is the `str()` of the `ValueError`/`TypeError` the executor reports
    /// as `configuration_error`.
    pub fn from_data(data: &PyJson) -> Result<SmtpConfig, String> {
        if let Some(message) = non_dict_lookup_error(data, "host", "host is required") {
            return Err(message);
        }
        let host = get(data, "host").cloned().unwrap_or(PyJson::Null);
        if !py_truthy(&host) {
            return Err("host is required".to_owned());
        }
        let verify = match get(data, "verify") {
            None | Some(PyJson::Null) => true,
            Some(PyJson::Bool(flag)) => *flag,
            Some(_) => return Err("verify must be a boolean".to_owned()),
        };
        let field = |key: &str| get(data, key);
        let text_or = |key: &str, default: &str| {
            field(key)
                .cloned()
                .unwrap_or_else(|| PyJson::Str(default.to_owned()))
        };

        // validate()
        let tls = match field("tls") {
            None => SmtpTls::Starttls,
            Some(PyJson::Str(mode)) if mode == "none" => SmtpTls::None,
            Some(PyJson::Str(mode)) if mode == "starttls" => SmtpTls::Starttls,
            Some(PyJson::Str(mode)) if mode == "implicit" => SmtpTls::Implicit,
            Some(value @ (PyJson::List(_) | PyJson::Dict(_))) => {
                return Err(format!("unhashable type: '{}'", py_type_name(value)));
            }
            Some(value) => {
                return Err(format!(
                    "Invalid tls mode: {}. Must be one of ['implicit', 'none', 'starttls']",
                    py_str(value)
                ));
            }
        };
        let port = match field("port").map_or(Some(IntValue::Small(587)), exact_int) {
            Some(IntValue::Small(n)) if (1..=65535).contains(&n) => n as u16,
            _ => return Err("port must be an integer between 1 and 65535".to_owned()),
        };
        let retries = match field("retries").map_or(Some(IntValue::Small(2)), exact_int) {
            Some(IntValue::Small(n)) if n >= 0 => n as u64,
            Some(IntValue::Big(false)) => u64::MAX,
            _ => return Err("retries must be a nonnegative integer".to_owned()),
        };
        let timeout = mail_duration("timeout", field("timeout").unwrap_or(&PyJson::Float(30.0)))?;
        let retry_delay = mail_duration(
            "retry_delay",
            field("retry_delay").unwrap_or(&PyJson::Float(5.0)),
        )?;
        let mut headers = Vec::with_capacity(3);
        for (name, value) in [
            ("from_addr", text_or("from_addr", "")),
            ("to_addr", text_or("to_addr", "")),
            ("subject_prefix", text_or("subject_prefix", "[nyxmon]")),
        ] {
            match value.as_str() {
                Some(text)
                    if !py_strip(text).is_empty()
                        && !text.contains('\r')
                        && !text.contains('\n') =>
                {
                    headers.push(text.to_owned());
                }
                _ => return Err(format!("{name} must be a nonempty string without CR/LF")),
            }
        }
        let username = field("username").cloned().unwrap_or(PyJson::Null);
        let password = field("password").cloned().unwrap_or(PyJson::Null);
        let password_secret = field("password_secret").cloned().unwrap_or(PyJson::Null);
        if py_truthy(&username) && !(py_truthy(&password) || py_truthy(&password_secret)) {
            return Err(
                "password or password_secret is required when username is provided".to_owned(),
            );
        }
        let subject_prefix = headers.pop().unwrap_or_default();
        let to_addr = headers.pop().unwrap_or_default();
        let from_addr = headers.pop().unwrap_or_default();
        Ok(SmtpConfig {
            host,
            port,
            tls,
            username,
            password,
            password_secret,
            from_addr,
            to_addr,
            subject_prefix,
            timeout,
            retries,
            retry_delay,
            verify,
        })
    }

    /// `get_password()`: the first truthy of `password`, `password_secret`.
    pub fn get_password(&self) -> Option<&PyJson> {
        [&self.password, &self.password_secret]
            .into_iter()
            .find(|value| py_truthy(value))
    }
}

/// The executor's steps before any I/O (`SmtpCheckExecutor.execute`):
/// configuration, subject and message. `make_msgid(domain)` builds the
/// Message-ID from the sender's domain (`_message_id_domain`). The error is
/// the `configuration_error` message.
pub fn prepare(
    data: &PyJson,
    now_unix: i64,
    token: &str,
    make_msgid: impl FnOnce(&str) -> String,
) -> Result<(SmtpConfig, SmtpMessage), String> {
    let config = SmtpConfig::from_data(data)?;
    let subject = build_subject(&config.subject_prefix, now_unix, token);
    let message = SmtpMessage::build(&config, &subject, token, make_msgid)?;
    Ok((config, message))
}

/// `_build_subject`: `"<prefix> <UTC ISO second>Z <token>".strip()`.
pub fn build_subject(prefix: &str, now_unix: i64, token: &str) -> String {
    py_strip(&format!("{prefix} {} {token}", iso_utc_z(now_unix))).to_owned()
}

/// `datetime.now(utc).replace(microsecond=0).isoformat()` with `Z`.
pub fn iso_utc_z(unix: i64) -> String {
    let days = unix.div_euclid(86_400);
    let secs = unix.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        secs / 3600,
        secs / 60 % 60,
        secs % 60
    )
}

/// Days since 1970-01-01 to a proleptic Gregorian date (H. Hinnant).
pub fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let year = yoe + era * 400 + i64::from(month <= 2);
    (year, month, day)
}

/// The body `set_content` stores (CRLF line ending added at flattening).
pub fn body_text(token: &str) -> String {
    format!("Nyxmon SMTP health check. Correlation token: {token}. Safe to delete.")
}

/// The `From`/`To` forms the Rust agent sends (PLAN.md 1.5 D-14). Python's
/// email policy re-renders and folds any header value; for these forms its
/// output is the configured text unchanged and the envelope is the bare
/// addresses, so no part of that policy needs porting:
///
/// - a mailbox is `local@domain` (both dot-atoms of RFC 5322 `atext`),
///   optionally as `Display Name <local@domain>` (the name words of atoms
///   and periods separated by single spaces, or one quoted string of
///   printable ASCII without `"` and `\`) or as `<local@domain>`;
/// - `from_addr` is one mailbox, `to_addr` one or more separated by `,` or
///   `, `;
/// - the value is printable ASCII without `=?` (no encoded words) and
///   `Name: value` fits one 78-character line.
///
/// Anything else (non-ASCII text or whitespace, comments, groups, quoted
/// local parts, domain literals, values Python repairs or folds) is a
/// `configuration_error` with [`unsupported_address`]: Python accepts such
/// values, but none occurs in production.
fn plain_addresses(header: &str, value: &str, list: bool) -> Option<Vec<String>> {
    let printable = value.bytes().all(|b| (0x20..0x7f).contains(&b));
    if !printable || value.contains("=?") || header.len() + 2 + value.len() > MAX_LINE {
        return None;
    }
    let mut addresses = Vec::new();
    for (index, element) in split_elements(value).into_iter().enumerate() {
        let element = if index > 0 {
            element.strip_prefix(' ').unwrap_or(element)
        } else {
            element
        };
        addresses.push(plain_mailbox(element)?.to_owned());
    }
    (list || addresses.len() == 1).then_some(addresses)
}

/// `value` split at the commas outside quoted strings.
fn split_elements(value: &str) -> Vec<&str> {
    let mut elements = Vec::new();
    let (mut start, mut quoted) = (0, false);
    for (index, byte) in value.bytes().enumerate() {
        match byte {
            b'"' => quoted = !quoted,
            b',' if !quoted => {
                elements.push(&value[start..index]);
                start = index + 1;
            }
            _ => {}
        }
    }
    elements.push(&value[start..]);
    elements
}

/// The address of one plain mailbox.
fn plain_mailbox(element: &str) -> Option<&str> {
    let Some(angled) = element.strip_suffix('>') else {
        return addr_spec(element);
    };
    let address = if let Some(rest) = angled.strip_prefix('"') {
        let (name, rest) = rest.split_once('"')?;
        if name.is_empty() || name.contains('\\') {
            return None;
        }
        rest.strip_prefix(" <")?
    } else if let Some(address) = angled.strip_prefix('<') {
        address
    } else {
        let (name, address) = angled.split_once(" <")?;
        if !name.split(' ').all(is_phrase_word) {
            return None;
        }
        address
    };
    addr_spec(address)
}

/// `local@domain` with dot-atom parts.
fn addr_spec(text: &str) -> Option<&str> {
    let (local, domain) = text.split_once('@')?;
    (is_dot_atom(local) && is_dot_atom(domain)).then_some(text)
}

fn is_atom(text: &str) -> bool {
    !text.is_empty()
        && text
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"!#$%&'*+-/=?^_`{|}~".contains(&b))
}

/// A word of an unquoted display name: atoms, also with periods (as in
/// `John Q. Public`).
fn is_phrase_word(text: &str) -> bool {
    !text.is_empty() && text.split('.').all(|part| part.is_empty() || is_atom(part))
}

fn is_dot_atom(text: &str) -> bool {
    text.split('.').all(is_atom)
}

/// The `configuration_error` for a `from_addr`/`to_addr` outside the plain
/// forms of [`plain_addresses`] (PLAN.md 1.5 D-14).
pub fn unsupported_address(field: &str) -> String {
    let form = if field == "to_addr" {
        "a plain ASCII address or a comma-separated list of them"
    } else {
        "a plain ASCII address"
    };
    format!(
        "{field} is not supported by the Rust agent: use {form} \
         (user@example.com or Name <user@example.com>) that fits one header line"
    )
}

const HEADER_LINE_ERROR: &str =
    "Header values may not contain linefeed or carriage return characters";

/// The test message of one SMTP check (`_build_message` plus what
/// `send_message` derives from it).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SmtpMessage {
    pub from_header: String,
    pub to_header: String,
    pub subject: String,
    pub message_id: String,
    pub token: String,
    /// `getaddresses([msg['From']])[0][1]`.
    pub envelope_from: String,
    /// `[a[1] for a in getaddresses([msg['To']])]`.
    pub recipients: Vec<String>,
}

impl SmtpMessage {
    /// `_build_message`. `make_msgid(domain)` returns the full Message-ID
    /// (`<time.pid.random@domain>`; the domain is the sender's, as
    /// `_message_id_domain` derives it from a plain address). The error is
    /// the `configuration_error` message: Python's header `ValueError`, or
    /// [`unsupported_address`].
    pub fn build(
        config: &SmtpConfig,
        subject: &str,
        token: &str,
        make_msgid: impl FnOnce(&str) -> String,
    ) -> Result<SmtpMessage, String> {
        for value in [&config.from_addr, &config.to_addr, subject] {
            if py_splitlines_count(value) > 1 {
                return Err(HEADER_LINE_ERROR.to_owned());
            }
        }
        let envelope_from = plain_addresses("From", &config.from_addr, false)
            .and_then(|mut addresses| addresses.pop())
            .ok_or_else(|| unsupported_address("from_addr"))?;
        let recipients = plain_addresses("To", &config.to_addr, true)
            .ok_or_else(|| unsupported_address("to_addr"))?;
        let domain = envelope_from
            .rsplit_once('@')
            .map_or("nyxmon.local", |(_, domain)| domain);
        let message_id = make_msgid(domain);
        if py_splitlines_count(&message_id) > 1 {
            return Err(HEADER_LINE_ERROR.to_owned());
        }
        Ok(SmtpMessage {
            from_header: config.from_addr.clone(),
            to_header: config.to_addr.clone(),
            subject: subject.to_owned(),
            message_id,
            token: token.to_owned(),
            envelope_from,
            recipients,
        })
    }

    /// `BytesGenerator.flatten(msg, linesep="\r\n")`. `From` and `To` are
    /// plain addresses on one line and the Message-ID is one token, so they
    /// are sent as they are; the `Subject` follows [`fold_subject`].
    pub fn flatten(&self) -> Vec<u8> {
        let mut out = String::new();
        out.push_str(&format!("From: {}\r\n", self.from_header));
        out.push_str(&format!("To: {}\r\n", self.to_header));
        out.push_str(&fold_subject(&self.subject));
        out.push_str(&format!("Message-ID: {}\r\n", self.message_id));
        out.push_str("Content-Type: text/plain; charset=\"utf-8\"\r\n");
        out.push_str("Content-Transfer-Encoding: 7bit\r\n");
        out.push_str("MIME-Version: 1.0\r\n\r\n");
        out.push_str(&body_text(&self.token));
        out.push_str("\r\n");
        out.into_bytes()
    }
}

const MAX_LINE: usize = 78;

/// The longest encoded-text of one RFC 2047 encoded word: 66 characters
/// with `=?utf-8?q??=`, so any encoded word fits a 78-character line after
/// the longest header name (`Message-ID: `).
const MAX_ENCODED_TEXT: usize = 54;

fn fits(name: &str, value: &str) -> bool {
    name.len() + 2 + value.chars().count() <= MAX_LINE
}

/// `Subject`: as configured when it is ASCII and fits one line; otherwise
/// the whole value as a series of encoded words, one per line, so it decodes
/// to exactly the subject (no separating whitespace is lost) and no line
/// exceeds 78 characters (P2: Python's policy folds such a subject
/// differently; wire bytes only, nothing of it is stored).
fn fold_subject(value: &str) -> String {
    if value.is_ascii() && fits("Subject", value) {
        return format!("Subject: {value}\r\n");
    }
    let words = encoded_words(value);
    format!("Subject: {}\r\n", words.join("\r\n "))
}

/// `text` as UTF-8 RFC 2047 encoded words of at most 66 characters each,
/// split between characters; each uses the shorter of `q` and `b`.
fn encoded_words(text: &str) -> Vec<String> {
    let mut words = Vec::new();
    let mut chunk = String::new();
    for c in text.chars() {
        let mut candidate = chunk.clone();
        candidate.push(c);
        let size = encode_q(&candidate).len().min(encode_b(&candidate).len());
        if !chunk.is_empty() && size > MAX_ENCODED_TEXT {
            words.push(encoded_word(&chunk));
            chunk.clear();
        }
        chunk.push(c);
    }
    if !chunk.is_empty() {
        words.push(encoded_word(&chunk));
    }
    words
}

/// RFC 2047 `Q` encoding (header variant: space as `_`).
fn encode_q(text: &str) -> String {
    let mut q = String::new();
    for &byte in text.as_bytes() {
        if byte.is_ascii_alphanumeric() || b"-!*+/".contains(&byte) {
            q.push(char::from(byte));
        } else if byte == b' ' {
            q.push('_');
        } else {
            q.push_str(&format!("={byte:02X}"));
        }
    }
    q
}

fn encode_b(text: &str) -> String {
    const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut b = String::new();
    for chunk in text.as_bytes().chunks(3) {
        let n = chunk
            .iter()
            .enumerate()
            .fold(0u32, |acc, (i, &v)| acc | (u32::from(v) << (16 - 8 * i)));
        for i in 0..4 {
            if i <= chunk.len() {
                b.push(char::from(B64[(n >> (18 - 6 * i) & 63) as usize]));
            } else {
                b.push('=');
            }
        }
    }
    b
}

/// The shorter of the `q` and `b` RFC 2047 encodings of `text` in UTF-8.
fn encoded_word(text: &str) -> String {
    let (q, b) = (encode_q(text), encode_b(text));
    if q.len() < b.len() {
        format!("=?utf-8?q?{q}?=")
    } else {
        format!("=?utf-8?b?{b}?=")
    }
}

#[cfg(test)]
mod tests;
