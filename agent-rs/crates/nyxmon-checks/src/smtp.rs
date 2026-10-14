//! SMTP check executor (`executors/smtp_executor.py`) with a hand-rolled
//! client that follows `smtplib` step by step (PLAN.md section 5.7): the
//! same commands, replies, exception classes and their mapping in
//! `SmtplibClient._send_blocking`.

use std::time::Duration;

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use hmac::{Hmac, Mac};
use indexmap::IndexMap;
use md5::Md5;
use nyxmon_core::config::smtp::{SmtpConfig, SmtpMessage, SmtpTls, prepare};
use nyxmon_core::model::{Check, CheckResult, ResultStatus};
use nyxmon_core::pyjson::{
    PyDict, PyJson, py_bytes_repr, py_int, py_isspace, py_str_repr, py_strip,
};
use nyxmon_core::pyjson::{py_str, py_type_name};
use rustls::ClientConfig;
use std::sync::Arc;

use crate::mail::MailEnv;
use crate::net::{Conn, MailStream, NetError, connect_tcp, os_error_text, tls_handshake};
use crate::tls;

/// `smtplib._MAXLINE`.
const MAX_LINE: usize = 8192;
/// `smtplib._MAXCHALLENGE`.
const MAX_CHALLENGE: u32 = 5;

/// The exception `smtplib` (or the socket layer) raised, by the class the
/// executor's `except` chain distinguishes.
#[derive(Debug, Clone, PartialEq, Eq)]
enum SmtpExc {
    /// `SMTPAuthenticationError(code, resp)`.
    Auth(i64, Vec<u8>),
    /// Any other `SMTPResponseException(code, resp)`.
    Response(i64, Vec<u8>),
    /// `socket.timeout` outside the reply/command I/O (connect, handshake).
    Timeout,
    /// `SMTPServerDisconnected`, `ConnectionRefusedError`, other `OSError`.
    Connection(String),
    /// Any other `SMTPException`.
    Smtp(String),
    /// `ssl.SSLError`.
    Tls(String),
    /// The executor's own `recipient_refused` error for the first refused
    /// recipient when others were accepted.
    RecipientRefused(i64, Vec<u8>),
    /// Not an SMTP or OS error (`ValueError`, `UnicodeEncodeError`, ...):
    /// escapes the client and becomes `unexpected_error`.
    Other(String),
}

impl From<NetError> for SmtpExc {
    fn from(err: NetError) -> Self {
        match err {
            NetError::Timeout(_) => SmtpExc::Timeout,
            NetError::Tls(text) => SmtpExc::Tls(text),
            NetError::Os(text) => SmtpExc::Connection(text),
            NetError::Other(text) => SmtpExc::Other(text),
        }
    }
}

/// `_decode_smtp_response`.
fn decode_response(data: &[u8]) -> String {
    match std::str::from_utf8(data) {
        Ok(text) => text.to_owned(),
        Err(_) => py_bytes_repr(data),
    }
}

/// The `UnicodeEncodeError` text of `text.encode("ascii")`.
fn ascii_error(text: &str) -> Option<String> {
    let chars: Vec<char> = text.chars().collect();
    let start = chars.iter().position(|c| !c.is_ascii())?;
    let end = chars[start..]
        .iter()
        .position(char::is_ascii)
        .map_or(chars.len(), |n| start + n);
    let code = chars[start] as u32;
    let first = if code <= 0xff {
        format!("'\\x{code:02x}'")
    } else if code <= 0xffff {
        format!("'\\u{code:04x}'")
    } else {
        format!("'\\U{code:08x}'")
    };
    Some(if end - start == 1 {
        format!(
            "'ascii' codec can't encode character {first} in position {start}: ordinal not in range(128)"
        )
    } else {
        format!(
            "'ascii' codec can't encode characters in position {start}-{}: ordinal not in range(128)",
            end - 1
        )
    })
}

fn encode_ascii(text: &str) -> Result<Vec<u8>, SmtpExc> {
    match ascii_error(text) {
        Some(message) => Err(SmtpExc::Other(message)),
        None => Ok(text.as_bytes().to_vec()),
    }
}

/// `value.encode("ascii")` where Python requires a `str`.
fn encode_str(value: &PyJson) -> Result<Vec<u8>, SmtpExc> {
    match value {
        PyJson::Str(text) => encode_ascii(text),
        other => Err(SmtpExc::Other(format!(
            "'{}' object has no attribute 'encode'",
            py_type_name(other)
        ))),
    }
}

/// `base64.decodebytes` (`binascii.a2b_base64`, non-strict, CPython
/// 3.13): characters outside the alphabet are skipped, padding only counts
/// after two or three data characters of a quad and does not end the input,
/// and leftover bits are ignored.
fn decode_base64(data: &[u8]) -> Result<Vec<u8>, SmtpExc> {
    let value = |b: u8| match b {
        b'A'..=b'Z' => Some(b - b'A'),
        b'a'..=b'z' => Some(b - b'a' + 26),
        b'0'..=b'9' => Some(b - b'0' + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    };
    let (mut quad, mut left, mut pads, mut count) = (0u8, 0u8, 0usize, 0usize);
    let mut out = Vec::new();
    for &byte in data {
        if byte == b'=' {
            if quad >= 2 {
                pads += 1;
            }
            continue;
        }
        let Some(v) = value(byte) else { continue };
        count += 1;
        pads = 0;
        match quad {
            0 => {
                left = v;
                quad = 1;
            }
            1 => {
                out.push((left << 2) | (v >> 4));
                left = v & 0x0f;
                quad = 2;
            }
            2 => {
                out.push((left << 4) | (v >> 2));
                left = v & 0x03;
                quad = 3;
            }
            _ => {
                out.push((left << 6) | v);
                quad = 0;
            }
        }
    }
    if quad == 1 {
        return Err(SmtpExc::Other(format!(
            "Invalid base64-encoded string: number of data characters ({count}) cannot be 1 more than a multiple of 4"
        )));
    }
    if quad >= 2 && usize::from(quad) + pads < 4 {
        return Err(SmtpExc::Other("Incorrect padding".to_owned()));
    }
    Ok(out)
}

#[derive(Clone, Copy)]
enum Mechanism {
    CramMd5,
    Plain,
    Login,
}

impl Mechanism {
    fn name(self) -> &'static str {
        match self {
            Mechanism::CramMd5 => "CRAM-MD5",
            Mechanism::Plain => "PLAIN",
            Mechanism::Login => "LOGIN",
        }
    }
}

/// One `smtplib.SMTP`/`SMTP_SSL` session.
struct Client {
    conn: Option<Conn>,
    host: String,
    local_hostname: String,
    esmtp: IndexMap<String, String>,
    does_esmtp: bool,
}

impl Client {
    fn close(&mut self) {
        self.conn = None;
    }

    async fn send(&mut self, data: &[u8]) -> Result<(), SmtpExc> {
        let Some(conn) = self.conn.as_mut() else {
            return Err(SmtpExc::Connection("please run connect() first".to_owned()));
        };
        if conn.send_all(data).await.is_err() {
            self.close();
            return Err(SmtpExc::Connection("Server not connected".to_owned()));
        }
        Ok(())
    }

    async fn putcmd(&mut self, cmd: &str, args: &str) -> Result<(), SmtpExc> {
        let line = if args.is_empty() {
            cmd.to_owned()
        } else {
            format!("{cmd} {args}")
        };
        if line.contains(['\r', '\n']) {
            let shown = line.replace('\n', "\\n").replace('\r', "\\r");
            return Err(SmtpExc::Other(format!(
                "command and arguments contain prohibited newline characters: {shown}"
            )));
        }
        let mut bytes = encode_ascii(&line)?;
        bytes.extend_from_slice(b"\r\n");
        self.send(&bytes).await
    }

    async fn getreply(&mut self) -> Result<(i64, Vec<u8>), SmtpExc> {
        let mut resp: Vec<Vec<u8>> = Vec::new();
        let mut errcode;
        loop {
            let Some(conn) = self.conn.as_mut() else {
                return Err(SmtpExc::Connection(
                    "Connection unexpectedly closed".to_owned(),
                ));
            };
            let line = match conn.read_line(MAX_LINE + 1).await {
                Ok(line) => line,
                Err(err) => {
                    self.close();
                    return Err(SmtpExc::Connection(format!(
                        "Connection unexpectedly closed: {}",
                        os_error_text(&err)
                    )));
                }
            };
            if line.is_empty() {
                self.close();
                return Err(SmtpExc::Connection(
                    "Connection unexpectedly closed".to_owned(),
                ));
            }
            if line.len() > MAX_LINE {
                self.close();
                return Err(SmtpExc::Response(500, b"Line too long.".to_vec()));
            }
            let text = line.get(4..).unwrap_or_default();
            let start = text.iter().position(|b| !b" \t\r\n".contains(b));
            let end = text.iter().rposition(|b| !b" \t\r\n".contains(b));
            resp.push(match (start, end) {
                (Some(s), Some(e)) => text[s..=e].to_vec(),
                _ => Vec::new(),
            });
            let code = &line[..line.len().min(3)];
            errcode = std::str::from_utf8(code)
                .ok()
                .filter(|c| c.is_ascii())
                .and_then(|c| py_int(c).ok())
                .and_then(|n| i64::try_from(n).ok());
            if errcode.is_none() || line.get(3) != Some(&b'-') {
                break;
            }
        }
        Ok((errcode.unwrap_or(-1), resp.join(&b"\n"[..])))
    }

    async fn docmd(&mut self, cmd: &str, args: &str) -> Result<(i64, Vec<u8>), SmtpExc> {
        self.putcmd(cmd, args).await?;
        self.getreply().await
    }

    fn has_extn(&self, name: &str) -> bool {
        self.esmtp.contains_key(&name.to_lowercase())
    }

    async fn ehlo(&mut self) -> Result<(), SmtpExc> {
        self.esmtp.clear();
        let name = self.local_hostname.clone();
        self.putcmd("ehlo", &name).await?;
        let (code, msg) = self.getreply().await?;
        if code == -1 && msg.is_empty() {
            self.close();
            return Err(SmtpExc::Connection("Server not connected".to_owned()));
        }
        if code != 250 {
            return Ok(());
        }
        self.does_esmtp = true;
        parse_ehlo_features(self, &msg);
        Ok(())
    }

    async fn starttls(&mut self, config: Arc<ClientConfig>) -> Result<(), SmtpExc> {
        if !self.has_extn("starttls") {
            return Err(SmtpExc::Smtp(
                "STARTTLS extension not supported by server.".to_owned(),
            ));
        }
        let (code, reply) = self.docmd("STARTTLS", "").await?;
        if code != 220 {
            return Err(SmtpExc::Response(code, reply));
        }
        let Some(conn) = self.conn.take() else {
            return Err(SmtpExc::Connection("please run connect() first".to_owned()));
        };
        let timeout = conn.timeout();
        let MailStream::Plain(tcp) = conn.into_stream() else {
            return Err(SmtpExc::Other("TLS session already established".to_owned()));
        };
        let stream = tls_handshake(tcp, &self.host, config, timeout).await?;
        self.conn = Some(Conn::new(stream, timeout));
        self.esmtp.clear();
        self.does_esmtp = false;
        Ok(())
    }

    /// `SMTP.auth(mechanism, authobject)`.
    async fn auth(
        &mut self,
        mechanism: Mechanism,
        user: &PyJson,
        password: &PyJson,
    ) -> Result<(), SmtpExc> {
        let plain = || encode_ascii(&format!("\0{}\0{}", py_str(user), py_str(password)));
        let initial = match mechanism {
            Mechanism::CramMd5 => None,
            Mechanism::Plain => Some(plain()?),
            Mechanism::Login => Some(encode_str(user)?),
        };
        let mut count: u32 = u32::from(initial.is_some());
        let (mut code, mut resp) = match initial {
            Some(bytes) => {
                let args = format!("{} {}", mechanism.name(), STANDARD.encode(bytes));
                self.docmd("AUTH", &args).await?
            }
            None => self.docmd("AUTH", mechanism.name()).await?,
        };
        while code == 334 {
            count += 1;
            let challenge = decode_base64(&resp)?;
            let response = match mechanism {
                Mechanism::CramMd5 => {
                    let key = encode_str(password)?;
                    let mut mac = Hmac::<Md5>::new_from_slice(&key)
                        .map_err(|err| SmtpExc::Other(err.to_string()))?;
                    mac.update(&challenge);
                    let digest: String = mac
                        .finalize()
                        .into_bytes()
                        .iter()
                        .map(|b| format!("{b:02x}"))
                        .collect();
                    encode_ascii(&format!("{} {digest}", py_str(user)))?
                }
                Mechanism::Plain => plain()?,
                Mechanism::Login if count < 2 => encode_str(user)?,
                Mechanism::Login => encode_str(password)?,
            };
            (code, resp) = self.docmd(&STANDARD.encode(response), "").await?;
            if count > MAX_CHALLENGE {
                return Err(SmtpExc::Smtp(format!(
                    "Server AUTH mechanism infinite loop. Last response: ({code}, {})",
                    py_bytes_repr(&resp)
                )));
            }
        }
        if code == 235 || code == 503 {
            Ok(())
        } else {
            Err(SmtpExc::Auth(code, resp))
        }
    }

    /// `SMTP.login(user, password)`.
    async fn login(&mut self, user: &PyJson, password: &PyJson) -> Result<(), SmtpExc> {
        if !self.has_extn("auth") {
            return Err(SmtpExc::Smtp(
                "SMTP AUTH extension not supported by server.".to_owned(),
            ));
        }
        let advertised: Vec<String> = self.esmtp["auth"]
            .split(py_isspace)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .collect();
        let mechanisms: Vec<Mechanism> = [Mechanism::CramMd5, Mechanism::Plain, Mechanism::Login]
            .into_iter()
            .filter(|m| advertised.iter().any(|a| a == m.name()))
            .collect();
        if mechanisms.is_empty() {
            return Err(SmtpExc::Smtp(
                "No suitable authentication method found.".to_owned(),
            ));
        }
        let mut last = None;
        for mechanism in mechanisms {
            match self.auth(mechanism, user, password).await {
                Ok(()) => return Ok(()),
                Err(err @ SmtpExc::Auth(..)) => last = Some(err),
                Err(err) => return Err(err),
            }
        }
        Err(last.unwrap_or(SmtpExc::Smtp("authentication failed".to_owned())))
    }

    /// `SMTP._rset`.
    async fn rset_quietly(&mut self) -> Result<(), SmtpExc> {
        match self.docmd("rset", "").await {
            Ok(_) | Err(SmtpExc::Connection(_)) => Ok(()),
            Err(err) => Err(err),
        }
    }

    /// `SMTP.send_message(msg)` + `sendmail`; returns the refused
    /// recipients.
    async fn send_message(
        &mut self,
        message: &SmtpMessage,
    ) -> Result<IndexMap<String, (i64, Vec<u8>)>, SmtpExc> {
        // Plain ASCII addresses only (PLAN.md 1.5 D-14): never SMTPUTF8.
        let flat = message.flatten();
        let mut option_list = String::new();
        if self.does_esmtp && self.has_extn("size") {
            option_list = format!(" size={}", flat.len());
        }
        let sender = &message.envelope_from;
        let (code, resp) = self
            .docmd("mail", &format!("from:<{sender}>{option_list}"))
            .await?;
        if code != 250 {
            if code == 421 {
                self.close();
            } else {
                self.rset_quietly().await?;
            }
            return Err(SmtpExc::Response(code, resp));
        }
        let mut refused: IndexMap<String, (i64, Vec<u8>)> = IndexMap::new();
        for recipient in &message.recipients {
            let (code, resp) = self.docmd("rcpt", &format!("to:<{recipient}>")).await?;
            if code != 250 && code != 251 {
                refused.insert(recipient.clone(), (code, resp));
            }
            if code == 421 {
                self.close();
                return Err(SmtpExc::Smtp(refused_repr(&refused)));
            }
        }
        if refused.len() == message.recipients.len() {
            self.rset_quietly().await?;
            return Err(SmtpExc::Smtp(refused_repr(&refused)));
        }
        // data()
        let (code, reply) = self.docmd("data", "").await?;
        if code != 354 {
            return Err(SmtpExc::Response(code, reply));
        }
        let mut quoted = quote_periods(&flat);
        if !quoted.ends_with(b"\r\n") {
            quoted.extend_from_slice(b"\r\n");
        }
        quoted.extend_from_slice(b".\r\n");
        self.send(&quoted).await?;
        let (code, resp) = self.getreply().await?;
        if code != 250 {
            if code == 421 {
                self.close();
            } else {
                self.rset_quietly().await?;
            }
            return Err(SmtpExc::Response(code, resp));
        }
        Ok(refused)
    }

    /// `quit()` then `close()`, errors ignored.
    async fn quit_quietly(&mut self) {
        if self.conn.is_some() {
            let _ = self.docmd("quit", "").await;
        }
        self.close();
    }
}

/// The feature parsing of `SMTP.ehlo()` (the reply decoded as latin-1,
/// first line skipped, old-style `AUTH=` lines merged into `auth`).
fn parse_ehlo_features(client: &mut Client, msg: &[u8]) {
    let text: String = msg.iter().map(|&b| char::from(b)).collect();
    for each in text.split('\n').skip(1) {
        if each.len() >= 5 && each.is_char_boundary(5) && each[..5].eq_ignore_ascii_case("auth=") {
            let entry = client.esmtp.entry("auth".to_owned()).or_default();
            entry.push(' ');
            entry.push_str(&each[5..]);
            continue;
        }
        let feature_len = each
            .char_indices()
            .take_while(|&(i, c)| c.is_ascii_alphanumeric() || (i > 0 && c == '-'))
            .count();
        if feature_len == 0 {
            continue;
        }
        let feature = each[..feature_len].to_ascii_lowercase();
        let params = py_strip(&each[feature_len..]).to_owned();
        if feature == "auth" {
            let entry = client.esmtp.entry(feature).or_default();
            entry.push(' ');
            entry.push_str(&params);
        } else {
            client.esmtp.insert(feature, params);
        }
    }
}

/// `str(SMTPRecipientsRefused(senderrs))`: the dict repr.
fn refused_repr(refused: &IndexMap<String, (i64, Vec<u8>)>) -> String {
    let items: Vec<String> = refused
        .iter()
        .map(|(addr, (code, resp))| {
            format!("{}: ({code}, {})", py_str_repr(addr), py_bytes_repr(resp))
        })
        .collect();
    format!("{{{}}}", items.join(", "))
}

/// `_quote_periods`: a `.` at the start of a line is doubled.
fn quote_periods(data: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(data.len() + 8);
    let mut line_start = true;
    for &byte in data {
        if line_start && byte == b'.' {
            out.push(b'.');
        }
        out.push(byte);
        line_start = byte == b'\n';
    }
    out
}

/// `SmtplibClient._send_blocking` up to the refused-recipients check.
async fn send_once(
    config: &SmtpConfig,
    message: &SmtpMessage,
    tls_config: Option<Arc<ClientConfig>>,
    env: &impl MailEnv,
) -> Result<(), SmtpExc> {
    let PyJson::Str(host) = &config.host else {
        return Err(SmtpExc::Other(
            "getaddrinfo() argument 1 must be string or None".to_owned(),
        ));
    };
    let timeout = Duration::from_secs_f64(config.timeout);
    let tcp = connect_tcp(host, config.port, timeout).await?;
    let stream = match (&config.tls, tls_config.clone()) {
        (SmtpTls::Implicit, Some(tls)) => tls_handshake(tcp, host, tls, timeout).await?,
        _ => MailStream::Plain(tcp),
    };
    let mut client = Client {
        conn: Some(Conn::new(stream, timeout)),
        host: host.clone(),
        local_hostname: String::new(),
        esmtp: IndexMap::new(),
        does_esmtp: false,
    };
    let (code, msg) = client.getreply().await?;
    if code != 220 {
        client.close();
        return Err(SmtpExc::Response(code, msg));
    }
    client.local_hostname = env.local_hostname().await;
    let outcome = session(&mut client, config, message, tls_config).await;
    client.quit_quietly().await;
    outcome
}

async fn session(
    client: &mut Client,
    config: &SmtpConfig,
    message: &SmtpMessage,
    tls_config: Option<Arc<ClientConfig>>,
) -> Result<(), SmtpExc> {
    client.ehlo().await?;
    if let (SmtpTls::Starttls, Some(tls)) = (config.tls, tls_config) {
        client.starttls(tls).await?;
        client.ehlo().await?;
    }
    if nyxmon_core::pyjson::py_truthy(&config.username) {
        // `get_password()` cannot be None after `validate()`.
        let password = config.get_password().cloned().unwrap_or(PyJson::Null);
        client.login(&config.username, &password).await?;
    }
    let refused = client.send_message(message).await?;
    match refused.into_iter().next() {
        Some((_, (code, resp))) => Err(SmtpExc::RecipientRefused(code, resp)),
        None => Ok(()),
    }
}

/// One `SmtpSendError` (or unexpected exception) as result data.
struct SendError {
    error_type: &'static str,
    message: String,
    code: Option<i64>,
    temporary: bool,
}

fn is_temporary(code: i64) -> bool {
    (400..500).contains(&code)
}

impl From<SmtpExc> for SendError {
    fn from(exc: SmtpExc) -> Self {
        let plain = |error_type, message| SendError {
            error_type,
            message,
            code: None,
            temporary: false,
        };
        match exc {
            SmtpExc::Auth(code, msg) => {
                let text = decode_response(&msg);
                SendError {
                    error_type: "auth_error",
                    message: if text.is_empty() {
                        "authentication failed".to_owned()
                    } else {
                        text
                    },
                    code: Some(code),
                    temporary: false,
                }
            }
            SmtpExc::RecipientRefused(code, resp) => SendError {
                error_type: "recipient_refused",
                message: decode_response(&resp),
                code: Some(code),
                temporary: is_temporary(code),
            },
            SmtpExc::Response(code, msg) => SendError {
                error_type: if is_temporary(code) {
                    "temporary_failure"
                } else {
                    "smtp_error"
                },
                message: decode_response(&msg),
                code: Some(code),
                temporary: is_temporary(code),
            },
            SmtpExc::Timeout => plain("timeout", "connection timed out".to_owned()),
            SmtpExc::Connection(text) => plain("connection_error", text),
            SmtpExc::Smtp(text) => plain("smtp_error", text),
            SmtpExc::Tls(text) => plain("tls_error", text),
            SmtpExc::Other(text) => plain("unexpected_error", text),
        }
    }
}

fn error_result(check_id: i64, error_type: &str, message: String) -> CheckResult {
    let mut data = PyDict::new();
    data.insert("error_type".to_owned(), PyJson::from(error_type));
    data.insert("error_msg".to_owned(), PyJson::from(message));
    CheckResult::new(check_id, ResultStatus::Error, data)
}

/// `SmtpCheckExecutor.execute`.
pub async fn execute(check: &Check, env: &impl MailEnv) -> CheckResult {
    let empty = PyJson::dict();
    let data = if check.data.is_null() {
        &empty
    } else {
        &check.data
    };
    let token = env.token();
    let now = env.now().floor() as i64;
    let prepared = prepare(data, now, &token, |domain| env.message_id(domain));
    let (config, message) = match prepared {
        Ok(pair) => pair,
        Err(message) => return error_result(check.check_id, "configuration_error", message),
    };
    let mut attempt: u64 = 0;
    loop {
        attempt += 1;
        let tls_config = (config.tls != SmtpTls::None).then(|| {
            tls::build_client_tls_config(config.verify, "SMTP", &py_str(&config.host), env.log())
        });
        let err = match send_once(&config, &message, tls_config, env).await {
            Ok(()) => {
                let mut data = PyDict::new();
                data.insert("response_code".to_owned(), PyJson::Int(250));
                data.insert("response_message".to_owned(), PyJson::from("accepted"));
                data.insert("attempts".to_owned(), attempt_json(attempt));
                data.insert("subject".to_owned(), PyJson::from(message.subject.as_str()));
                data.insert("token".to_owned(), PyJson::from(token.as_str()));
                data.insert("from".to_owned(), PyJson::from(config.from_addr.as_str()));
                data.insert("to".to_owned(), PyJson::from(config.to_addr.as_str()));
                return CheckResult::new(check.check_id, ResultStatus::Ok, data);
            }
            Err(exc) => SendError::from(exc),
        };
        if err.temporary && attempt <= config.retries {
            env.sleep(Duration::from_secs_f64(config.retry_delay)).await;
            continue;
        }
        let mut result = error_result(check.check_id, err.error_type, err.message);
        result
            .data
            .insert("attempts".to_owned(), attempt_json(attempt));
        if let Some(code) = err.code {
            result
                .data
                .insert("smtp_code".to_owned(), PyJson::Int(code));
        }
        return result;
    }
}

fn attempt_json(attempt: u64) -> PyJson {
    PyJson::from_bigint(attempt.into())
}

#[cfg(test)]
mod tests;
