//! The plain-text STARTTLS dialogues of the TCP check
//! (`tcp_executor.py:_send_starttls_command`, `_LineReader`, `_starttls_*`).
//!
//! On success the server has accepted STARTTLS and the next bytes on the
//! stream belong to the TLS handshake. Every byte limit, regex and message
//! is Python's; `fixtures/starttls_text.json` replays scripted dialogues
//! through both implementations.

use std::sync::LazyLock;
use std::time::Duration;

use nyxmon_core::config::tcp::{StarttlsProtocol, TcpConfig};
use nyxmon_core::pyjson::{PyDict, PyJson, py_isdigit, py_lower, py_strip, py_type_name};
use regex::Regex;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

use crate::tcp::{AttemptError, TcpCheckError};

/// `SMTP_EHLO_NAME`.
pub const SMTP_EHLO_NAME: &str = "nyxmon.invalid";
/// `IMAP_TAG`.
pub const IMAP_TAG: &str = "a1";
/// `MAX_STARTTLS_LINE_BYTES`: the longest line and the size of one read.
pub const MAX_STARTTLS_LINE_BYTES: usize = 4096;
/// `MAX_STARTTLS_DIALOGUE_BYTES`.
pub const MAX_STARTTLS_DIALOGUE_BYTES: usize = 64 * 1024;
/// `MAX_STARTTLS_RESPONSE_CHARS`.
pub const MAX_STARTTLS_RESPONSE_CHARS: usize = 500;

/// `_IMAP_OK_GREETING` (`re.IGNORECASE`, anchored like `re.match`).
pub static IMAP_OK_GREETING: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)^\* OK(?:[ \[]|$)").expect("valid regex"));
/// `_IMAP_UNTAGGED_BYE`.
pub static IMAP_UNTAGGED_BYE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)^\* BYE(?:[ \[]|$)").expect("valid regex"));
/// `_IMAP_TAGGED`.
pub static IMAP_TAGGED: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)^a1 (OK|NO|BAD)(?:[ \[]|$)").expect("valid regex"));
/// `_SIEVE_RESPONSE`.
pub static SIEVE_RESPONSE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)^(OK|NO|BYE)(?:[ (]|$)").expect("valid regex"));

/// `_truncate`: at most 500 characters, then `...`.
pub fn truncate(text: &str) -> String {
    match text.char_indices().nth(MAX_STARTTLS_RESPONSE_CHARS) {
        None => text.to_owned(),
        Some((end, _)) => format!("{}...", &text[..end]),
    }
}

/// `TcpCheckExecutor._is_positive_starttls_response`.
pub fn is_positive_starttls_response(response: &str) -> bool {
    let normalized = py_lower(response);
    let numeric_code = normalized.split(' ').next().unwrap_or("");
    if !numeric_code.is_empty() && numeric_code.chars().all(py_isdigit) {
        return numeric_code.starts_with('2');
    }
    normalized.starts_with('2') || normalized.contains("ok")
}

/// `bytes.decode("utf-8", errors=...)`: invalid sequences replaced by
/// U+FFFD (`replace`) or dropped (`ignore`).
fn decode_utf8(data: &[u8], replace: bool) -> String {
    let mut out = String::with_capacity(data.len());
    for chunk in data.utf8_chunks() {
        out.push_str(chunk.valid());
        if replace && !chunk.invalid().is_empty() {
            out.push('\u{fffd}');
        }
    }
    out
}

/// `_starttls_rejected`: the error for a negative or unexpected reply.
pub fn starttls_rejected(stage: &str, lines: &[String]) -> TcpCheckError {
    let response = truncate(py_strip(&lines.join("\n")));
    let shown = if response.is_empty() {
        "no response"
    } else {
        &response
    };
    let message = format!("STARTTLS rejected at {stage}: {shown}");
    rejected_error(message, response, stage)
}

fn rejected_error(message: String, response: String, stage: &str) -> TcpCheckError {
    let mut data = PyDict::new();
    data.insert("starttls_response".to_owned(), PyJson::Str(response));
    data.insert("starttls_stage".to_owned(), PyJson::Str(stage.to_owned()));
    TcpCheckError::with_data("starttls_rejected", message, false, data)
}

fn protocol_error(message: String) -> TcpCheckError {
    TcpCheckError::new("starttls_protocol_error", message, false)
}

/// Why a dialogue step failed.
#[derive(Debug)]
pub enum DialogueError {
    /// A structured check error (rejection, protocol error).
    Check(TcpCheckError),
    /// End of stream or a socket error (`EndOfStream`, `OSError`, ...).
    Closed,
    /// Any other exception (`str(exc)`), e.g. a non-string command.
    Exception(String),
}

impl From<TcpCheckError> for DialogueError {
    fn from(err: TcpCheckError) -> Self {
        DialogueError::Check(err)
    }
}

/// `_LineReader`: a size-capped line reader for the plain-text dialogue.
pub struct LineReader<'a, S> {
    stream: &'a mut S,
    buffer: Vec<u8>,
    received: usize,
}

impl<'a, S: AsyncRead + AsyncWrite + Unpin> LineReader<'a, S> {
    pub fn new(stream: &'a mut S) -> Self {
        LineReader {
            stream,
            buffer: Vec::new(),
            received: 0,
        }
    }

    /// Bytes received but not yet consumed.
    pub fn pending(&self) -> &[u8] {
        &self.buffer
    }

    async fn send(&mut self, data: &[u8]) -> Result<(), DialogueError> {
        self.stream
            .write_all(data)
            .await
            .map_err(|_| DialogueError::Closed)?;
        self.stream.flush().await.map_err(|_| DialogueError::Closed)
    }

    /// The next line without its line ending (`rstrip(b"\r\n")`), decoded
    /// with `errors="replace"`.
    async fn readline(&mut self) -> Result<String, DialogueError> {
        loop {
            if let Some(newline) = self.buffer.iter().position(|&b| b == b'\n') {
                if newline + 1 > MAX_STARTTLS_LINE_BYTES {
                    return Err(Self::too_long());
                }
                let line: Vec<u8> = self.buffer.drain(..=newline).collect();
                let end = line
                    .iter()
                    .rposition(|&b| b != b'\r' && b != b'\n')
                    .map_or(0, |index| index + 1);
                return Ok(decode_utf8(&line[..end], true));
            }
            if self.buffer.len() >= MAX_STARTTLS_LINE_BYTES {
                return Err(Self::too_long());
            }
            self.fill().await?;
        }
    }

    /// Buffered bytes, or the next chunk from the stream.
    async fn receive_some(&mut self) -> Result<Vec<u8>, DialogueError> {
        if self.buffer.is_empty() {
            self.fill().await?;
        }
        Ok(std::mem::take(&mut self.buffer))
    }

    async fn fill(&mut self) -> Result<(), DialogueError> {
        let mut chunk = [0u8; MAX_STARTTLS_LINE_BYTES];
        let read = self
            .stream
            .read(&mut chunk)
            .await
            .map_err(|_| DialogueError::Closed)?;
        if read == 0 {
            return Err(DialogueError::Closed);
        }
        self.received += read;
        if self.received > MAX_STARTTLS_DIALOGUE_BYTES {
            return Err(protocol_error(format!(
                "STARTTLS dialogue exceeded {MAX_STARTTLS_DIALOGUE_BYTES} bytes before TLS started"
            ))
            .into());
        }
        self.buffer.extend_from_slice(&chunk[..read]);
        Ok(())
    }

    fn too_long() -> DialogueError {
        protocol_error(format!(
            "STARTTLS dialogue line longer than {MAX_STARTTLS_LINE_BYTES} bytes"
        ))
        .into()
    }
}

/// `_starttls_generic`: optionally read the greeting, send the configured
/// command and classify the first chunk of the reply.
async fn starttls_generic<S: AsyncRead + AsyncWrite + Unpin>(
    reader: &mut LineReader<'_, S>,
    config: &TcpConfig,
) -> Result<(), DialogueError> {
    if config.starttls_read_greeting {
        reader.readline().await?;
    }
    let command = match &config.starttls_command {
        PyJson::Str(command) => command.as_bytes(),
        other => {
            return Err(DialogueError::Exception(format!(
                "'{}' object has no attribute 'encode'",
                py_type_name(other)
            )));
        }
    };
    reader.send(command).await?;
    let response = reader.receive_some().await?;
    let decoded = decode_utf8(&response, false);
    let text = py_strip(&decoded);
    if is_positive_starttls_response(text) {
        return Ok(());
    }
    let shown = if text.is_empty() { "no response" } else { text };
    Err(rejected_error(
        format!("STARTTLS rejected: {shown}"),
        truncate(text),
        "starttls",
    )
    .into())
}

/// `_expect_smtp_reply`: a (multi-line) SMTP reply with `expected_code`.
async fn expect_smtp_reply<S: AsyncRead + AsyncWrite + Unpin>(
    reader: &mut LineReader<'_, S>,
    expected_code: &str,
    stage: &str,
) -> Result<(), DialogueError> {
    let mut lines = Vec::new();
    let code = loop {
        let line = reader.readline().await?;
        let code: String = line.chars().take(3).collect();
        let separator: String = line.chars().skip(3).take(1).collect();
        lines.push(line);
        let numeric = !code.is_empty() && code.chars().all(py_isdigit);
        if !numeric || !matches!(separator.as_str(), "" | " " | "-") {
            return Err(starttls_rejected(stage, &lines).into());
        }
        if separator != "-" {
            break code;
        }
    };
    if code != expected_code {
        return Err(starttls_rejected(stage, &lines).into());
    }
    Ok(())
}

/// `_starttls_smtp`: 220 greeting, EHLO, STARTTLS, expect 220 (RFC 3207).
async fn starttls_smtp<S: AsyncRead + AsyncWrite + Unpin>(
    reader: &mut LineReader<'_, S>,
) -> Result<(), DialogueError> {
    expect_smtp_reply(reader, "220", "greeting").await?;
    reader
        .send(format!("EHLO {SMTP_EHLO_NAME}\r\n").as_bytes())
        .await?;
    expect_smtp_reply(reader, "250", "ehlo").await?;
    reader.send(b"STARTTLS\r\n").await?;
    expect_smtp_reply(reader, "220", "starttls").await
}

/// `_starttls_imap`: `* OK` greeting, `a1 STARTTLS`, expect `a1 OK`.
async fn starttls_imap<S: AsyncRead + AsyncWrite + Unpin>(
    reader: &mut LineReader<'_, S>,
) -> Result<(), DialogueError> {
    let greeting = reader.readline().await?;
    if !IMAP_OK_GREETING.is_match(&greeting) {
        return Err(starttls_rejected("greeting", &[greeting]).into());
    }
    reader
        .send(format!("{IMAP_TAG} STARTTLS\r\n").as_bytes())
        .await?;
    let mut lines = Vec::new();
    loop {
        let line = reader.readline().await?;
        lines.push(line);
        let line = lines.last().map(String::as_str).unwrap_or_default();
        if IMAP_UNTAGGED_BYE.is_match(line) {
            return Err(starttls_rejected("starttls", &lines).into());
        }
        if line.starts_with("* ") {
            continue;
        }
        let ok = IMAP_TAGGED
            .captures(line)
            .and_then(|captures| captures.get(1))
            .is_some_and(|status| status.as_str().to_uppercase() == "OK");
        if ok {
            return Ok(());
        }
        return Err(starttls_rejected("starttls", &lines).into());
    }
}

/// `_expect_sieve_ok`: skip capability lines until OK/NO/BYE; require OK.
async fn expect_sieve_ok<S: AsyncRead + AsyncWrite + Unpin>(
    reader: &mut LineReader<'_, S>,
    stage: &str,
) -> Result<(), DialogueError> {
    let mut lines = Vec::new();
    loop {
        let line = reader.readline().await?;
        let status = SIEVE_RESPONSE
            .captures(&line)
            .and_then(|captures| captures.get(1))
            .map(|status| status.as_str().to_uppercase());
        lines.push(line);
        match status {
            None => continue,
            Some(status) if status == "OK" => return Ok(()),
            Some(_) => return Err(starttls_rejected(stage, &lines).into()),
        }
    }
}

/// `_starttls_sieve`: capabilities + OK, STARTTLS, expect OK (RFC 5804).
async fn starttls_sieve<S: AsyncRead + AsyncWrite + Unpin>(
    reader: &mut LineReader<'_, S>,
) -> Result<(), DialogueError> {
    expect_sieve_ok(reader, "greeting").await?;
    reader.send(b"STARTTLS\r\n").await?;
    expect_sieve_ok(reader, "starttls").await
}

/// The dialogue of `config.starttls_protocol`, without a time limit.
pub async fn dialogue<S: AsyncRead + AsyncWrite + Unpin>(
    reader: &mut LineReader<'_, S>,
    config: &TcpConfig,
) -> Result<(), DialogueError> {
    match config.starttls_protocol {
        StarttlsProtocol::Generic => starttls_generic(reader, config).await,
        StarttlsProtocol::Smtp => starttls_smtp(reader).await,
        StarttlsProtocol::Imap => starttls_imap(reader).await,
        StarttlsProtocol::Sieve => starttls_sieve(reader).await,
    }
}

/// `_send_starttls_command`: run the dialogue bounded by `timeout` and
/// refuse plain-text bytes received after the STARTTLS reply.
///
/// # Errors
/// `starttls_timeout`, `starttls_connection_closed` (both retryable),
/// `starttls_rejected`, `starttls_protocol_error`, or an unexpected error.
pub async fn send_starttls_command<S: AsyncRead + AsyncWrite + Unpin>(
    stream: &mut S,
    config: &TcpConfig,
    timeout: Duration,
) -> Result<(), AttemptError> {
    let mut reader = LineReader::new(stream);
    match tokio::time::timeout(timeout, dialogue(&mut reader, config)).await {
        Err(_) => Err(TcpCheckError::new(
            "starttls_timeout",
            "Timed out waiting for STARTTLS response".to_owned(),
            true,
        )
        .into()),
        Ok(Err(DialogueError::Closed)) => Err(TcpCheckError::new(
            "starttls_connection_closed",
            "Connection closed during the STARTTLS dialogue".to_owned(),
            true,
        )
        .into()),
        Ok(Err(DialogueError::Check(err))) => Err(err.into()),
        Ok(Err(DialogueError::Exception(message))) => Err(AttemptError::Unexpected(message)),
        Ok(Ok(())) if !reader.pending().is_empty() => Err(protocol_error(
            "Server sent unexpected data after the STARTTLS reply".to_owned(),
        )
        .into()),
        Ok(Ok(())) => Ok(()),
    }
}

#[cfg(test)]
mod tests;
