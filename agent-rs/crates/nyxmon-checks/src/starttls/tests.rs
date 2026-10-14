//! `fixtures/starttls_text.json`: the classification helpers, the patterns
//! and scripted dialogues replayed through the Rust dialogue.

use std::collections::VecDeque;
use std::io;
use std::pin::Pin;
use std::task::{Context, Poll};

use nyxmon_core::config::ConfigOutcome;
use nyxmon_core::pyjson::loads;
use tokio::io::ReadBuf;

use super::*;

fn fixture() -> PyJson {
    loads(include_str!("../../../../fixtures/starttls_text.json")).expect("valid fixture")
}

fn text<'a>(value: &'a PyJson, key: &str) -> &'a str {
    value
        .get(key)
        .and_then(PyJson::as_str)
        .unwrap_or_else(|| panic!("{key} missing in {value:?}"))
}

fn list<'a>(value: &'a PyJson, key: &str) -> &'a [PyJson] {
    value
        .get(key)
        .and_then(PyJson::as_list)
        .unwrap_or_else(|| panic!("{key} missing in {value:?}"))
}

fn hex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
        .collect()
}

#[test]
fn positive_response_heuristic_matches() {
    for case in list(&fixture(), "positive") {
        let response = text(case, "text");
        let expected = case.get("positive").and_then(PyJson::as_bool).unwrap();
        assert_eq!(
            is_positive_starttls_response(response),
            expected,
            "{response:?}"
        );
    }
}

#[test]
fn truncation_matches() {
    for case in list(&fixture(), "truncate") {
        assert_eq!(truncate(text(case, "text")), text(case, "truncated"));
    }
}

#[test]
fn patterns_match() {
    let fixture = fixture();
    let patterns = fixture.get("patterns").unwrap();
    for (name, regex) in [
        ("imap_ok_greeting", &*IMAP_OK_GREETING),
        ("imap_untagged_bye", &*IMAP_UNTAGGED_BYE),
        ("imap_tagged", &*IMAP_TAGGED),
        ("sieve_response", &*SIEVE_RESPONSE),
    ] {
        for row in list(patterns, name) {
            let line = text(row, "line");
            let captures = regex.captures(line);
            let matched = row.get("match").and_then(PyJson::as_bool).unwrap();
            assert_eq!(captures.is_some(), matched, "{name} {line:?}");
            let group = captures.as_ref().and_then(|c| c.get(1)).map(|m| m.as_str());
            assert_eq!(
                group,
                row.get("group").and_then(PyJson::as_str),
                "{name} {line:?}"
            );
        }
    }
}

/// Replays scripted chunks (each read returns at most the buffer size) and
/// records what is written.
struct ScriptedStream {
    chunks: VecDeque<Vec<u8>>,
    reset_at_end: bool,
    sent: Vec<Vec<u8>>,
}

impl AsyncRead for ScriptedStream {
    fn poll_read(
        mut self: Pin<&mut Self>,
        _cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        match self.chunks.pop_front() {
            Some(mut chunk) => {
                let take = chunk.len().min(buf.remaining());
                buf.put_slice(&chunk[..take]);
                if take < chunk.len() {
                    let rest = chunk.split_off(take);
                    self.chunks.push_front(rest);
                }
                Poll::Ready(Ok(()))
            }
            None if self.reset_at_end => {
                Poll::Ready(Err(io::Error::from(io::ErrorKind::ConnectionReset)))
            }
            None => Poll::Ready(Ok(())),
        }
    }
}

impl AsyncWrite for ScriptedStream {
    fn poll_write(
        mut self: Pin<&mut Self>,
        _cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        self.sent.push(buf.to_vec());
        Poll::Ready(Ok(buf.len()))
    }

    fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(Ok(()))
    }

    fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(Ok(()))
    }
}

fn config_for(case: &PyJson) -> TcpConfig {
    let read_greeting = case.get("read_greeting").and_then(PyJson::as_bool).unwrap();
    let mut data = PyDict::new();
    data.insert("port".to_owned(), PyJson::Int(1));
    data.insert("tls_mode".to_owned(), PyJson::Str("starttls".to_owned()));
    data.insert(
        "starttls_protocol".to_owned(),
        PyJson::Str(text(case, "protocol").to_owned()),
    );
    data.insert(
        "starttls_command".to_owned(),
        loads(text(case, "command_json")).unwrap(),
    );
    data.insert(
        "starttls_read_greeting".to_owned(),
        PyJson::Bool(read_greeting),
    );
    match TcpConfig::from_data(&PyJson::Dict(data)) {
        ConfigOutcome::Ok(config) => config,
        other => panic!("invalid dialogue config: {other:?}"),
    }
}

#[tokio::test]
async fn scripted_dialogues_match() {
    let fixture = fixture();
    let dialogues = list(&fixture, "dialogues");
    assert!(dialogues.len() > 60);
    for case in dialogues {
        let config = config_for(case);
        let mut stream = ScriptedStream {
            chunks: list(case, "chunks")
                .iter()
                .map(|c| hex(c.as_str().unwrap()))
                .collect(),
            reset_at_end: text(case, "end") == "reset",
            sent: Vec::new(),
        };
        let outcome = send_starttls_command(&mut stream, &config, Duration::from_secs(10)).await;
        let context = format!("{case:?}");
        let sent: Vec<Vec<u8>> = list(case, "sent")
            .iter()
            .map(|s| hex(s.as_str().unwrap()))
            .collect();
        assert_eq!(stream.sent, sent, "{context}");
        let expected = case.get("outcome").unwrap();
        match (text(expected, "kind"), outcome) {
            ("ok", Ok(())) => {}
            ("error", Err(AttemptError::Check(err))) => {
                assert_eq!(err.error_type, text(expected, "error_type"), "{context}");
                assert_eq!(err.message, text(expected, "message"), "{context}");
                assert_eq!(
                    Some(err.retryable),
                    expected.get("retryable").and_then(PyJson::as_bool),
                    "{context}"
                );
                let data = match expected.get("data") {
                    Some(PyJson::Dict(data)) => data.clone(),
                    _ => PyDict::new(),
                };
                assert_eq!(err.data, data, "{context}");
            }
            ("exception", Err(AttemptError::Unexpected(message))) => {
                assert_eq!(message, text(expected, "message"), "{context}");
            }
            (kind, outcome) => panic!("{context}: expected {kind}, got {outcome:?}"),
        }
    }
}

#[tokio::test(start_paused = true)]
async fn a_silent_server_times_out() {
    let (mut client, _server) = tokio::io::duplex(64);
    let config = config_for(
        &loads(r#"{"protocol": "smtp", "read_greeting": false, "command_json": "\"\""}"#).unwrap(),
    );
    let outcome = send_starttls_command(&mut client, &config, Duration::from_secs(3)).await;
    let Err(AttemptError::Check(err)) = outcome else {
        panic!("expected a timeout, got {outcome:?}");
    };
    assert_eq!(err.error_type, "starttls_timeout");
    assert_eq!(err.message, "Timed out waiting for STARTTLS response");
    assert!(err.retryable);
}
