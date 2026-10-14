//! IMAP check executor (`executors/imap_executor.py`) on a client that
//! follows `imaplib.IMAP4` (CPython 3.13) step by step: the same commands
//! and argument quoting, the response parser with its untagged-response
//! store, and the same exceptions (PLAN.md section 5.14).

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use indexmap::IndexMap;
use nyxmon_core::config::imap::{
    ImapConfig, ImapTls, InternalDate, fetch_uid, iso_utc, parse_internaldate, search_ids,
};
use nyxmon_core::config::{ConfigError, PyException};
use nyxmon_core::model::{Check, CheckResult, ResultStatus};
use nyxmon_core::pyjson::{
    PyDict, PyJson, py_bytes_repr, py_isspace, py_str, py_str_repr, py_type_name,
};
use rustls::ClientConfig;
use tokio::net::TcpStream;

use crate::mail::MailEnv;
use crate::net::{Conn, MailStream, NetError, connect_tcp, os_error_text, tls_handshake};
use crate::smtp::ascii_error;
use crate::tls;

/// `imaplib._MAXLINE`.
const MAX_LINE: usize = 1_000_000;

/// An exception raised inside the session, by the class the executor's
/// `except` chain distinguishes.
#[derive(Clone, Debug, PartialEq, Eq)]
enum ImapExc {
    /// `ImapCheckError` (`_run_checked`): `execution_error`.
    Check(String),
    /// `ssl.SSLError`: `tls_error`.
    Tls(String),
    /// `imaplib.error`/`abort`/`readonly`; `command: X => ` wrapping
    /// applies to these only.
    Lib(String),
    /// Any other exception (`OSError`, `TimeoutError`, `ValueError`, ...).
    Other(String),
}

impl From<NetError> for ImapExc {
    fn from(err: NetError) -> Self {
        match err {
            NetError::Tls(text) => ImapExc::Tls(text),
            NetError::Timeout(text) | NetError::Os(text) | NetError::Other(text) => {
                ImapExc::Other(text)
            }
        }
    }
}

fn io_exc(err: &std::io::Error) -> ImapExc {
    match err
        .get_ref()
        .and_then(|e| e.downcast_ref::<rustls::Error>())
    {
        Some(tls) => ImapExc::Tls(tls.to_string()),
        None => ImapExc::Other(os_error_text(err)),
    }
}

/// One element of an `imaplib` data list.
#[derive(Clone, Debug, PartialEq, Eq)]
enum Datum {
    Bytes(Vec<u8>),
    /// `(header, literal)` for a response with a `{n}` literal.
    Literal(Vec<u8>, Vec<u8>),
}

/// An `imaplib` data list; `None` is Python's `None` (`[None]`).
type Data = Vec<Option<Datum>>;

/// `repr(data)` of a data list.
fn data_repr(data: &Data) -> String {
    let items: Vec<String> = data
        .iter()
        .map(|item| match item {
            None => "None".to_owned(),
            Some(Datum::Bytes(b)) => py_bytes_repr(b),
            Some(Datum::Literal(h, l)) => format!("({}, {})", py_bytes_repr(h), py_bytes_repr(l)),
        })
        .collect();
    format!("[{}]", items.join(", "))
}

/// `str.encode("ascii")` of a value Python requires to be a `str`.
fn ascii(text: &str) -> Result<Vec<u8>, ImapExc> {
    match ascii_error(text) {
        Some(message) => Err(ImapExc::Other(message)),
        None => Ok(text.as_bytes().to_vec()),
    }
}

/// `IMAP4._quote`.
fn quote(value: &PyJson) -> Result<Vec<u8>, ImapExc> {
    let PyJson::Str(text) = value else {
        return Err(ImapExc::Other(format!(
            "'{}' object has no attribute 'replace'",
            py_type_name(value)
        )));
    };
    let mut out = vec![b'"'];
    for byte in ascii(text)? {
        if byte == b'\\' || byte == b'"' {
            out.push(b'\\');
        }
        out.push(byte);
    }
    out.push(b'"');
    Ok(out)
}

/// `_quoted.fullmatch(arg)`: `"(?:[^"\\]|\\.)*+"`.
fn is_quoted(arg: &[u8]) -> bool {
    if arg.len() < 2 || arg[0] != b'"' {
        return false;
    }
    let mut pos = 1;
    while pos < arg.len() {
        match arg[pos] {
            b'"' => return pos == arg.len() - 1,
            b'\\' if pos + 1 < arg.len() && arg[pos + 1] != b'\n' => pos += 2,
            b'\\' => return false,
            _ => pos += 1,
        }
    }
    false
}

/// `IMAP4._astring`.
fn astring(value: &PyJson) -> Result<Vec<u8>, ImapExc> {
    let PyJson::Str(text) = value else {
        return Err(ImapExc::Other(format!(
            "expected string or bytes-like object, got '{}'",
            py_type_name(value)
        )));
    };
    let arg = ascii(text)?;
    let special = |b: &u8| b"(){ %*\\\"".contains(b) || *b < 0x20 || *b >= 0x7f;
    if is_quoted(&arg) || (!arg.is_empty() && !arg.iter().any(special)) {
        return Ok(arg);
    }
    quote(value)
}

/// `imaplib.Int2AP`.
fn int2ap(mut num: u32) -> String {
    let mut out = Vec::new();
    while num > 0 {
        out.push(b"ABCDEFGHIJKLMNOP"[(num % 16) as usize]);
        num /= 16;
    }
    out.reverse();
    String::from_utf8(out).unwrap_or_default()
}

/// The states in which `imaplib.Commands` allows a command we send.
fn allowed_states(name: &str) -> &'static [&'static str] {
    match name {
        "CAPABILITY" | "LOGOUT" => &["NONAUTH", "AUTH", "SELECTED", "LOGOUT"],
        "LOGIN" | "STARTTLS" => &["NONAUTH"],
        "SELECT" => &["AUTH", "SELECTED"],
        _ => &["SELECTED"],
    }
}

/// `str(data, "ascii")` / `bytes.decode("ascii", "replace")`.
fn ascii_text(data: &[u8]) -> Result<String, ImapExc> {
    match data.iter().position(|b| !b.is_ascii()) {
        Some(pos) => Err(ImapExc::Other(format!(
            "'ascii' codec can't decode byte 0x{:02x} in position {pos}: ordinal not in range(128)",
            data[pos]
        ))),
        None => Ok(String::from_utf8_lossy(data).into_owned()),
    }
}

fn ascii_replace(data: &[u8]) -> String {
    data.iter()
        .map(|&b| {
            if b.is_ascii() {
                char::from(b)
            } else {
                '\u{fffd}'
            }
        })
        .collect()
}

/// Leading `[A-Z-]+` run.
fn atom_len(data: &[u8]) -> usize {
    data.iter()
        .take_while(|b| b.is_ascii_uppercase() || **b == b'-')
        .count()
}

/// `Response_code.match(dat)`: `\[(TYPE)( (DATA))?\]` with a greedy DATA.
fn response_code(dat: &[u8]) -> Option<(String, Option<Vec<u8>>)> {
    let rest = dat.strip_prefix(b"[")?;
    let len = atom_len(rest);
    if len == 0 {
        return None;
    }
    let kind = String::from_utf8_lossy(&rest[..len]).into_owned();
    let after = &rest[len..];
    if after.first() == Some(&b' ')
        && let Some(close) = after.iter().rposition(|&b| b == b']')
    {
        return Some((kind, Some(after[1..close].to_vec())));
    }
    (after.first() == Some(&b']')).then_some((kind, None))
}

/// `_paren_depth`: parentheses outside quoted strings.
fn paren_depth(data: &[u8], mut depth: i64) -> i64 {
    let mut pos = 0;
    while pos < data.len() {
        if data[pos] == b'"' {
            // `"(?:[^"\\]|\\.)*+"` removed by `_quoted.sub`.
            let mut end = pos + 1;
            let mut closed = None;
            while end < data.len() {
                match data[end] {
                    b'"' => {
                        closed = Some(end);
                        break;
                    }
                    b'\\' if end + 1 < data.len() && data[end + 1] != b'\n' => end += 2,
                    b'\\' => break,
                    _ => end += 1,
                }
            }
            if let Some(close) = closed {
                pos = close + 1;
                continue;
            }
        }
        match data[pos] {
            b'(' => depth += 1,
            b')' => depth -= 1,
            _ => {}
        }
        pos += 1;
    }
    depth
}

/// `.*{(\d+)}$` on a response line: the literal size.
fn literal_size(dat: &[u8]) -> Option<usize> {
    let body = dat.strip_suffix(b"}")?;
    let open = body.iter().rposition(|&b| b == b'{')?;
    let digits = &body[open + 1..];
    if digits.is_empty() || !digits.iter().all(u8::is_ascii_digit) {
        return None;
    }
    std::str::from_utf8(digits).ok()?.parse().ok()
}

/// One `imaplib.IMAP4` connection.
struct Imap {
    conn: Option<Conn>,
    host: String,
    timeout: Duration,
    tagpre: String,
    tagnum: u64,
    tagged: HashMap<String, Option<(String, Vec<u8>)>>,
    untagged: IndexMap<String, Vec<Datum>>,
    capabilities: Vec<String>,
    state: &'static str,
    tls_established: bool,
}

impl Imap {
    /// `IMAP4(host, port, timeout)` / `IMAP4_SSL(...)`: connect, read the
    /// greeting and the capabilities.
    async fn open(
        host: &str,
        port: u16,
        timeout: Duration,
        tls_config: Option<Arc<ClientConfig>>,
    ) -> Result<Imap, ImapExc> {
        let tcp = connect_tcp(host, port, timeout).await?;
        let stream = match tls_config {
            Some(config) => tls_handshake(tcp, host, config, timeout).await?,
            None => MailStream::Plain(tcp),
        };
        let mut imap = Imap {
            conn: Some(Conn::new(stream, timeout)),
            host: host.to_owned(),
            timeout,
            tagpre: int2ap(4096 + rand::random::<u32>() % 61_440),
            tagnum: 0,
            tagged: HashMap::new(),
            untagged: IndexMap::new(),
            capabilities: Vec::new(),
            state: "LOGOUT",
            tls_established: false,
        };
        imap.connect().await?;
        Ok(imap)
    }

    /// `IMAP4._connect`.
    async fn connect(&mut self) -> Result<(), ImapExc> {
        let (welcome, line) = loop {
            let line = self.get_line().await?;
            if !line.is_empty() {
                let welcome = self.handle_response(line.clone()).await?;
                break (welcome, line);
            }
        };
        if self.untagged.contains_key("PREAUTH") {
            self.state = "AUTH";
        } else if self.untagged.contains_key("OK") {
            self.state = "NONAUTH";
        } else {
            let shown = welcome.unwrap_or(line);
            return Err(ImapExc::Lib(format!(
                "invalid greeting: {}",
                ascii_replace(&shown)
            )));
        }
        self.refresh_capabilities(true).await?;
        if self
            .capabilities
            .iter()
            .any(|c| c == "IMAP4REV1" || c == "IMAP4")
        {
            Ok(())
        } else {
            Err(ImapExc::Lib("server not IMAP4 compliant".to_owned()))
        }
    }

    /// `IMAP4._get_line`.
    async fn get_line(&mut self) -> Result<Vec<u8>, ImapExc> {
        let Some(conn) = self.conn.as_mut() else {
            return Err(ImapExc::Lib("socket error: EOF".to_owned()));
        };
        let mut line = conn.read_line(MAX_LINE + 1).await.map_err(|e| io_exc(&e))?;
        if line.len() > MAX_LINE {
            return Err(ImapExc::Lib(format!("got more than {MAX_LINE} bytes")));
        }
        if line.is_empty() {
            return Err(ImapExc::Lib("socket error: EOF".to_owned()));
        }
        if !line.ends_with(b"\r\n") {
            return Err(ImapExc::Lib(format!(
                "socket error: unterminated line: {}",
                py_bytes_repr(&line)
            )));
        }
        line.truncate(line.len() - 2);
        Ok(line)
    }

    /// `IMAP4._get_response`: `None` for a continuation.
    async fn get_response(&mut self) -> Result<Option<Vec<u8>>, ImapExc> {
        let mut resp = self.get_line().await?;
        while resp.is_empty() {
            resp = self.get_line().await?;
        }
        self.handle_response(resp).await
    }

    fn append_untagged(&mut self, kind: String, datum: Datum) {
        self.untagged.entry(kind).or_default().push(datum);
    }

    /// The tagged-response match (`tagre`): `(tag, type, data)`.
    fn match_tagged(&self, resp: &[u8]) -> Option<(String, String, Vec<u8>)> {
        let rest = resp.strip_prefix(self.tagpre.as_bytes())?;
        let digits = rest.iter().take_while(|b| b.is_ascii_digit()).count();
        if digits == 0 || rest.get(digits) != Some(&b' ') {
            return None;
        }
        let after = &rest[digits + 1..];
        let len = after.iter().take_while(|b| b.is_ascii_uppercase()).count();
        if len == 0 || after.get(len) != Some(&b' ') {
            return None;
        }
        let tag = String::from_utf8_lossy(&resp[..self.tagpre.len() + digits]).into_owned();
        let kind = String::from_utf8_lossy(&after[..len]).into_owned();
        Some((tag, kind, after[len + 1..].to_vec()))
    }

    async fn handle_response(&mut self, resp: Vec<u8>) -> Result<Option<Vec<u8>>, ImapExc> {
        let (kind, mut dat);
        if let Some((tag, typ, data)) = self.match_tagged(&resp) {
            if !self.tagged.contains_key(&tag) {
                return Err(ImapExc::Lib(format!(
                    "unexpected tagged response: {}",
                    py_bytes_repr(&resp)
                )));
            }
            self.tagged.insert(tag, Some((typ.clone(), data.clone())));
            (kind, dat) = (typ, data);
        } else {
            let (typ, first, second) = if let Some(rest) = resp.strip_prefix(b"* ") {
                let len = atom_len(rest);
                let digits = rest.iter().take_while(|b| b.is_ascii_digit()).count();
                if len > 0 {
                    let data = rest[len..].strip_prefix(b" ").map(<[u8]>::to_vec);
                    (rest[..len].to_vec(), data, None)
                } else if digits > 0
                    && rest.get(digits) == Some(&b' ')
                    && atom_len(&rest[digits + 1..]) > 0
                {
                    let after = &rest[digits + 1..];
                    let tlen = atom_len(after);
                    let data2 = after[tlen..].strip_prefix(b" ").map(<[u8]>::to_vec);
                    (after[..tlen].to_vec(), Some(rest[..digits].to_vec()), data2)
                } else {
                    return Err(ImapExc::Lib(format!(
                        "unexpected response: {}",
                        py_bytes_repr(&resp)
                    )));
                }
            } else if resp.first() == Some(&b'+') {
                return Ok(None);
            } else {
                return Err(ImapExc::Lib(format!(
                    "unexpected response: {}",
                    py_bytes_repr(&resp)
                )));
            };
            kind = String::from_utf8_lossy(&typ).into_owned();
            dat = first.unwrap_or_default();
            if let Some(second) = second.filter(|s| !s.is_empty()) {
                dat.push(b' ');
                dat.extend_from_slice(&second);
            }
            let mut depth = 0;
            while let Some(size) = literal_size(&dat) {
                let literal = match self.conn.as_mut() {
                    Some(conn) => conn.read_exact_or_eof(size).await.map_err(|e| io_exc(&e))?,
                    None => Vec::new(),
                };
                depth = paren_depth(&dat, depth);
                self.append_untagged(kind.clone(), Datum::Literal(dat, literal));
                dat = self.get_line().await?;
                while dat.is_empty() && depth > 0 {
                    dat = self.get_line().await?;
                }
            }
            self.append_untagged(kind.clone(), Datum::Bytes(dat.clone()));
        }
        if matches!(kind.as_str(), "OK" | "NO" | "BAD")
            && let Some((code, data)) = response_code(&dat)
        {
            self.append_untagged(code, Datum::Bytes(data.unwrap_or_default()));
        }
        Ok(Some(resp))
    }

    /// `IMAP4._check_bye`.
    fn check_bye(&self) -> Result<(), ImapExc> {
        match self.untagged.get("BYE").and_then(|b| b.last()) {
            Some(Datum::Bytes(text)) => Err(ImapExc::Lib(ascii_replace(text))),
            Some(Datum::Literal(..)) => Err(ImapExc::Other(
                "'tuple' object has no attribute 'decode'".to_owned(),
            )),
            None => Ok(()),
        }
    }

    /// `IMAP4._command`: returns the tag.
    async fn command(&mut self, name: &str, args: &[Vec<u8>]) -> Result<String, ImapExc> {
        let states = allowed_states(name);
        if !states.contains(&self.state) {
            return Err(ImapExc::Lib(format!(
                "command {name} illegal in state {}, only allowed in states {}",
                self.state,
                states.join(", ")
            )));
        }
        for kind in ["OK", "NO", "BAD"] {
            self.untagged.shift_remove(kind);
        }
        if self.untagged.contains_key("READ-ONLY") {
            return Err(ImapExc::Lib(
                "mailbox status changed to READ-ONLY".to_owned(),
            ));
        }
        let tag = format!("{}{}", self.tagpre, self.tagnum);
        self.tagnum += 1;
        self.tagged.insert(tag.clone(), None);
        let mut data = format!("{tag} {name}").into_bytes();
        for arg in args {
            if arg.iter().any(|b| matches!(b, b'\0' | b'\r' | b'\n')) {
                return Err(ImapExc::Other(
                    "NUL, CR and LF not allowed in commands".to_owned(),
                ));
            }
            data.push(b' ');
            data.extend_from_slice(arg);
        }
        data.extend_from_slice(b"\r\n");
        let sent = match self.conn.as_mut() {
            Some(conn) => conn.send_all(&data).await,
            None => Err(std::io::Error::other("Bad file descriptor")),
        };
        if let Err(err) = sent {
            return Err(ImapExc::Lib(format!(
                "socket error: {}",
                os_error_text(&err)
            )));
        }
        Ok(tag)
    }

    /// `IMAP4._command_complete`.
    async fn command_complete(&mut self, name: &str, tag: &str) -> Result<(String, Data), ImapExc> {
        let logout = name == "LOGOUT";
        if !logout {
            self.check_bye()?;
        }
        let (kind, data) = match self.tagged_response(tag, logout).await {
            Ok(result) => result,
            Err(ImapExc::Lib(text)) => {
                return Err(ImapExc::Lib(format!("command: {name} => {text}")));
            }
            Err(err) => return Err(err),
        };
        if !logout {
            self.check_bye()?;
        }
        if kind == "BAD" {
            return Err(ImapExc::Lib(format!(
                "{name} command error: {kind} {}",
                data_repr(&data)
            )));
        }
        Ok((kind, data))
    }

    /// `IMAP4._get_tagged_response`.
    async fn tagged_response(
        &mut self,
        tag: &str,
        expect_bye: bool,
    ) -> Result<(String, Data), ImapExc> {
        loop {
            if let Some(Some((kind, dat))) = self.tagged.get(tag).cloned() {
                self.tagged.remove(tag);
                return Ok((kind, vec![Some(Datum::Bytes(dat))]));
            }
            if expect_bye && let Some(bye) = self.untagged.shift_remove("BYE") {
                return Ok(("BYE".to_owned(), bye.into_iter().map(Some).collect()));
            }
            self.check_bye()?;
            self.get_response().await?;
        }
    }

    async fn simple_command(
        &mut self,
        name: &str,
        args: &[Vec<u8>],
    ) -> Result<(String, Data), ImapExc> {
        let tag = self.command(name, args).await?;
        self.command_complete(name, &tag).await
    }

    /// `IMAP4._untagged_response`.
    fn untagged_response(&mut self, kind: String, data: Data, name: &str) -> (String, Data) {
        if kind == "NO" {
            return (kind, data);
        }
        match self.untagged.shift_remove(name) {
            Some(items) => (kind, items.into_iter().map(Some).collect()),
            None => (kind, vec![None]),
        }
    }

    /// `IMAP4.capability()`.
    async fn capability(&mut self) -> Result<(String, Data), ImapExc> {
        let (kind, data) = self.simple_command("CAPABILITY", &[]).await?;
        Ok(self.untagged_response(kind, data, "CAPABILITY"))
    }

    fn set_capabilities(&mut self, datum: Option<&Datum>) -> Result<(), ImapExc> {
        match datum {
            Some(Datum::Bytes(bytes)) => {
                let text = ascii_text(bytes)?.to_ascii_uppercase();
                self.capabilities = text
                    .split(py_isspace)
                    .filter(|word| !word.is_empty())
                    .map(str::to_owned)
                    .collect();
                Ok(())
            }
            Some(Datum::Literal(..)) => Err(ImapExc::Other(
                "decoding to str: need a bytes-like object, tuple found".to_owned(),
            )),
            None => Err(ImapExc::Lib(
                "no CAPABILITY response from server".to_owned(),
            )),
        }
    }

    /// `IMAP4._get_capabilities`.
    async fn get_capabilities(&mut self) -> Result<(), ImapExc> {
        let (_, data) = self.capability().await?;
        if data == vec![None] {
            return Err(ImapExc::Lib(
                "no CAPABILITY response from server".to_owned(),
            ));
        }
        self.set_capabilities(data.last().and_then(Option::as_ref))
    }

    /// `IMAP4._refresh_capabilities`.
    async fn refresh_capabilities(&mut self, consume: bool) -> Result<(), ImapExc> {
        match self
            .untagged
            .get("CAPABILITY")
            .and_then(|c| c.last())
            .cloned()
        {
            Some(datum) => {
                if consume {
                    self.untagged.shift_remove("CAPABILITY");
                }
                self.set_capabilities(Some(&datum))
            }
            None => self.get_capabilities().await,
        }
    }

    /// `IMAP4.starttls(ssl_context)`.
    async fn starttls(&mut self, config: Arc<ClientConfig>) -> Result<(), ImapExc> {
        if self.tls_established {
            return Err(ImapExc::Lib("TLS session already established".to_owned()));
        }
        if !self.capabilities.iter().any(|c| c == "STARTTLS") {
            return Err(ImapExc::Lib("TLS not supported by server".to_owned()));
        }
        let (kind, _) = self.simple_command("STARTTLS", &[]).await?;
        if kind != "OK" {
            return Err(ImapExc::Lib("Couldn't establish TLS session".to_owned()));
        }
        let conn = self.conn.take();
        let tcp: Option<TcpStream> = match conn.map(Conn::into_stream) {
            Some(MailStream::Plain(tcp)) => Some(tcp),
            _ => None,
        };
        let Some(tcp) = tcp else {
            return Err(ImapExc::Lib("socket error: EOF".to_owned()));
        };
        let stream = tls_handshake(tcp, &self.host, config, self.timeout).await?;
        self.conn = Some(Conn::new(stream, self.timeout));
        self.tls_established = true;
        self.get_capabilities().await
    }

    /// `IMAP4.login(user, password)`.
    async fn login(&mut self, user: &PyJson, password: &PyJson) -> Result<(String, Data), ImapExc> {
        let args = [astring(user)?, quote(password)?];
        let (kind, data) = self.simple_command("LOGIN", &args).await?;
        if kind != "OK" {
            let text = match data.last() {
                Some(Some(Datum::Bytes(bytes))) => String::from_utf8_lossy(bytes).into_owned(),
                _ => String::new(),
            };
            return Err(ImapExc::Lib(text));
        }
        self.state = "AUTH";
        self.refresh_capabilities(false).await?;
        Ok((kind, data))
    }

    /// `IMAP4.select(mailbox)`.
    async fn select(&mut self, mailbox: &PyJson) -> Result<(String, Data), ImapExc> {
        self.untagged.clear();
        let arg = astring(mailbox)?;
        let (kind, data) = self.simple_command("SELECT", &[arg]).await?;
        if kind != "OK" {
            self.state = "AUTH";
            return Ok((kind, data));
        }
        self.state = "SELECTED";
        if self.untagged.contains_key("READ-ONLY") {
            return Err(ImapExc::Lib(format!(
                "{} is not writable",
                py_str_repr(mailbox.as_str().unwrap_or_default())
            )));
        }
        let exists = match self.untagged.get("EXISTS") {
            Some(items) => items.iter().cloned().map(Some).collect(),
            None => vec![None],
        };
        Ok((kind, exists))
    }

    /// `IMAP4.uid(command, *args)` for SEARCH, FETCH, STORE and EXPUNGE.
    async fn uid(&mut self, command: &str, args: Vec<Vec<u8>>) -> Result<(String, Data), ImapExc> {
        if self.state != "SELECTED" {
            return Err(ImapExc::Lib(format!(
                "command {command} illegal in state {}, only allowed in states SELECTED",
                self.state
            )));
        }
        let mut all = vec![command.as_bytes().to_vec()];
        all.extend(args);
        let (kind, data) = self.simple_command("UID", &all).await?;
        let name = if command == "SEARCH" {
            "SEARCH"
        } else {
            "FETCH"
        };
        Ok(self.untagged_response(kind, data, name))
    }

    /// `IMAP4.logout()`.
    async fn logout(&mut self) -> Result<(), ImapExc> {
        self.state = "LOGOUT";
        self.simple_command("LOGOUT", &[]).await?;
        self.conn = None;
        Ok(())
    }
}

/// `ImapLibSession._run_checked`.
fn checked(method: &str, (kind, data): (String, Data)) -> Result<Data, ImapExc> {
    if kind == "OK" {
        Ok(data)
    } else {
        Err(ImapExc::Check(format!(
            "{method} failed: {kind} {}",
            data_repr(&data)
        )))
    }
}

/// What one attempt ended with besides a result.
enum AttemptError {
    NoRecent(String),
    Exc(ImapExc),
}

impl From<ImapExc> for AttemptError {
    fn from(exc: ImapExc) -> Self {
        AttemptError::Exc(exc)
    }
}

/// `ImapLibSession.search_recent`: `(uid, internaldate)` per match.
async fn search_recent(
    imap: &mut Imap,
    subject: &PyJson,
    max_age_minutes: i64,
    env: &impl MailEnv,
) -> Result<Vec<(String, i64)>, ImapExc> {
    let criterion = ascii(&format!("\"{}\"", py_str(subject)))?;
    let args = ["NOT", "DELETED", "HEADER", "SUBJECT"]
        .iter()
        .map(|a| a.as_bytes().to_vec())
        .chain([criterion])
        .collect();
    let data = checked("uid", imap.uid("SEARCH", args).await?)?;
    let ids: Vec<String> = match data.first() {
        Some(Some(Datum::Bytes(bytes))) if !bytes.is_empty() => {
            search_ids(bytes).map_err(ImapExc::Other)?
        }
        Some(Some(Datum::Literal(..))) => {
            return Err(ImapExc::Other(
                "'tuple' object has no attribute 'decode'".to_owned(),
            ));
        }
        _ => Vec::new(),
    };
    if ids.is_empty() {
        return Ok(Vec::new());
    }
    // `datetime.now()` is read after the SEARCH completed.
    let cutoff = env.now() - (max_age_minutes * 60) as f64;
    let mut messages = Vec::new();
    for id in ids {
        let args = vec![ascii(&id)?, b"(INTERNALDATE)".to_vec()];
        let data = checked("uid", imap.uid("FETCH", args).await?)?;
        let raw = data.iter().find_map(|entry| {
            let candidate = match entry {
                Some(Datum::Bytes(b)) | Some(Datum::Literal(b, _)) => b,
                None => return None,
            };
            (fetch_uid(candidate) == Some(id.as_bytes())).then_some(candidate)
        });
        let Some(raw) = raw else { continue };
        match parse_internaldate(raw) {
            InternalDate::At(at) if (at as f64) >= cutoff => messages.push((id, at)),
            InternalDate::At(_) | InternalDate::Missing => {}
            InternalDate::Error(text) => return Err(ImapExc::Other(text)),
        }
    }
    Ok(messages)
}

/// `ImapLibSession.delete_messages`.
async fn delete_messages(imap: &mut Imap, ids: &[String]) -> Result<(), ImapExc> {
    let set = ascii(&ids.join(","))?;
    let args = vec![set.clone(), b"+FLAGS".to_vec(), b"(\\Deleted)".to_vec()];
    checked("uid", imap.uid("STORE", args).await?)?;
    let capabilities = checked("capability", imap.capability().await?)?;
    let mut uidplus = false;
    for line in capabilities.iter().flatten() {
        match line {
            Datum::Bytes(bytes) if !bytes.is_empty() => {
                uidplus |= bytes
                    .to_ascii_uppercase()
                    .split(|b| b" \t\n\r\x0b\x0c".contains(b))
                    .any(|word| word == b"UIDPLUS");
            }
            Datum::Bytes(_) => {}
            Datum::Literal(..) => {
                return Err(ImapExc::Other(
                    "'tuple' object has no attribute 'upper'".to_owned(),
                ));
            }
        }
        if uidplus {
            break;
        }
    }
    if uidplus {
        checked("uid", imap.uid("EXPUNGE", vec![set]).await?)?;
    }
    Ok(())
}

/// `ImapCheckExecutor._run_once`.
async fn run_once(
    check: &Check,
    config: &ImapConfig,
    env: &impl MailEnv,
) -> Result<CheckResult, AttemptError> {
    let host = config.session_host().map_err(ImapExc::Other)?;
    let timeout = Duration::from_secs_f64(config.timeout);
    let tls_config = || tls::build_client_tls_config(config.verify, "IMAP", &host, env.log());
    // __aenter__: connect, login, select (no LOGOUT when it fails).
    let mut imap = match config.tls_mode {
        ImapTls::Implicit => Imap::open(&host, config.port, timeout, Some(tls_config())).await?,
        ImapTls::None => Imap::open(&host, config.port, timeout, None).await?,
        ImapTls::Starttls => {
            let mut imap = Imap::open(&host, config.port, timeout, None).await?;
            imap.starttls(tls_config()).await?;
            imap
        }
    };
    checked(
        "login",
        imap.login(&config.username, &config.password).await?,
    )?;
    checked("select", imap.select(&config.folder).await?)?;
    let outcome = session(&mut imap, config, env).await;
    // __aexit__: best-effort LOGOUT.
    let _ = imap.logout().await;
    let (uids, latest) = outcome?;
    let mut data = PyDict::new();
    data.insert(
        "matched_uids".to_owned(),
        PyJson::List(uids.into_iter().map(PyJson::Str).collect()),
    );
    data.insert(
        "latest_internaldate".to_owned(),
        PyJson::Str(iso_utc(latest)),
    );
    Ok(CheckResult::new(check.check_id, ResultStatus::Ok, data))
}

async fn session(
    imap: &mut Imap,
    config: &ImapConfig,
    env: &impl MailEnv,
) -> Result<(Vec<String>, i64), AttemptError> {
    let mut messages =
        search_recent(imap, &config.search_subject, config.max_age_minutes, env).await?;
    if messages.is_empty() {
        return Err(AttemptError::NoRecent(format!(
            "No messages with subject '{}' within {} minutes",
            py_str(&config.search_subject),
            config.max_age_minutes
        )));
    }
    messages.sort_by_key(|(_, at)| *at);
    let latest = messages.last().map_or(0, |(_, at)| *at);
    let uids: Vec<String> = messages.into_iter().map(|(uid, _)| uid).collect();
    if config.delete_after_check {
        delete_messages(imap, &uids).await?;
    }
    Ok((uids, latest))
}

fn error_data(error_type: &str, message: String) -> PyDict {
    let mut data = PyDict::new();
    data.insert("error_type".to_owned(), PyJson::from(error_type));
    data.insert("error_msg".to_owned(), PyJson::Str(message));
    data
}

/// `ImapCheckExecutor.execute`. `Err` is an exception that escapes the
/// executor (`executor_exception` in the runner).
pub async fn execute(check: &Check, env: &impl MailEnv) -> Result<CheckResult, PyException> {
    let empty = PyJson::dict();
    let data = if check.data.is_null() {
        &empty
    } else {
        &check.data
    };
    let config = match ImapConfig::from_data(data) {
        Ok(config) => config,
        Err(ConfigError::Invalid(message)) => {
            let data = error_data("configuration_error", message);
            return Ok(CheckResult::new(check.check_id, ResultStatus::Error, data));
        }
        Err(ConfigError::Exception(exc)) => return Err(exc),
    };
    let mut attempt: u32 = 0;
    loop {
        attempt += 1;
        let (error_type, message, status, severity) = match run_once(check, &config, env).await {
            Ok(result) => return Ok(result),
            Err(AttemptError::NoRecent(message)) => {
                if attempt <= config.retries {
                    env.sleep(Duration::from_secs_f64(config.retry_delay)).await;
                    continue;
                }
                let status = if config.warning_severity {
                    ResultStatus::Warning
                } else {
                    ResultStatus::Error
                };
                (
                    "no_recent_message",
                    message,
                    status,
                    Some(config.severity()),
                )
            }
            Err(AttemptError::Exc(exc)) => {
                let (kind, text) = match exc {
                    ImapExc::Check(text) => ("execution_error", text),
                    ImapExc::Tls(text) => ("tls_error", text),
                    ImapExc::Lib(text) | ImapExc::Other(text) => ("unexpected_error", text),
                };
                (kind, text, ResultStatus::Error, None)
            }
        };
        let mut data = error_data(error_type, message);
        data.insert("attempts".to_owned(), PyJson::Int(i64::from(attempt)));
        if let Some(severity) = severity {
            data.insert("severity".to_owned(), PyJson::from(severity));
        }
        return Ok(CheckResult::new(check.check_id, status, data));
    }
}

#[cfg(test)]
mod tests;
