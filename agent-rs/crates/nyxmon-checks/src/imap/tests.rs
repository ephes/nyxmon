//! The IMAP executor against scripted fake servers: the dialogues of
//! `fixtures/imap_dialogues.json` (the Python executor with the real
//! `imaplib` against the same scripts), and Rust-only tests for TLS and
//! timeouts. Transcripts drop the random tag prefix `imaplib` uses.

use std::time::Duration;

use nyxmon_core::config::PyException;
use nyxmon_core::model::{Check, ResultStatus};
use nyxmon_core::pyjson::{PyJson, loads};

use super::*;
use crate::fake_server::{ScriptServer, Step, TestEnv, send, tagged};

const GREETING: &str = "* OK [CAPABILITY IMAP4rev1 UIDPLUS] ready\r\n";

fn check_data(data: PyJson) -> Check {
    Check {
        check_id: 9,
        service_id: 1,
        name: "imap".to_owned(),
        check_type: "imap".to_owned(),
        url: "imap.example.com".to_owned(),
        check_interval: 300,
        next_check_time: 0,
        processing_started_at: 0,
        status: "processing".to_owned(),
        disabled: false,
        data,
        claim_started_at: 0,
    }
}

fn check(port: u16, extra: &str) -> Check {
    let text = format!(
        r#"{{"host": "127.0.0.1", "port": {port}, "tls_mode": "none",
            "username": "monitor", "password": "secret", "search_subject": "[nyxmon]",
            "retries": 0, "retry_delay": 7, "timeout": 5{extra}}}"#
    );
    check_data(loads(&text).expect("valid check data"))
}

fn untag(lines: Vec<String>) -> Vec<String> {
    lines
        .into_iter()
        .map(|line| {
            line.split_once(' ')
                .map_or(line.clone(), |(_, rest)| rest.to_owned())
        })
        .collect()
}

fn latin1(text: &str) -> Vec<u8> {
    text.chars().map(|c| c as u8).collect()
}

fn script_step(step: &PyJson) -> Step {
    let step = step.as_list().expect("step list");
    let text = || latin1(step[1].as_str().expect("step text"));
    match step[0].as_str() {
        Some("send") => Step::Bytes(text()),
        Some("tagged") => Step::Tagged(text()),
        Some("recv") => Step::Recv,
        Some("close") => Step::Close,
        other => panic!("unknown step {other:?}"),
    }
}

fn keys(data: &PyDict) -> Vec<&str> {
    data.keys().map(String::as_str).collect()
}

/// Every scripted dialogue gives Python's result (status, data, key order),
/// retry delays and wire transcript.
#[tokio::test]
async fn dialogues_match_the_python_executor() {
    let fixture = loads(include_str!("../../../../fixtures/imap_dialogues.json")).unwrap();
    let now = match fixture.get("now") {
        Some(PyJson::Float(now)) => *now,
        other => panic!("now: {other:?}"),
    };
    for case in fixture.get("cases").and_then(PyJson::as_list).unwrap() {
        let name = case.get("name").and_then(PyJson::as_str).unwrap();
        let scripts: Vec<Vec<Step>> = case
            .get("scripts")
            .and_then(PyJson::as_list)
            .unwrap()
            .iter()
            .map(|script| script.as_list().unwrap().iter().map(script_step).collect())
            .collect();
        let server = ScriptServer::start(scripts, false).await;
        let mut data = case.get("data").unwrap().clone();
        data.as_dict_mut()
            .unwrap()
            .insert("port".to_owned(), PyJson::Int(i64::from(server.port)));
        let env = TestEnv {
            now,
            ..TestEnv::default()
        };
        let result = execute(&check_data(data), &env)
            .await
            .unwrap_or_else(|exc| panic!("{name}: {exc:?}"));
        let transcripts: Vec<Vec<String>> =
            server.transcripts().await.into_iter().map(untag).collect();

        let status = case.get("status").and_then(PyJson::as_str).unwrap();
        assert_eq!(result.status.as_str(), status, "{name}");
        let expected = case.get("result").and_then(PyJson::as_dict).unwrap();
        let actual = PyJson::Dict(result.data.clone()).to_json_string().unwrap();
        let mut sorted = result.data.clone();
        sorted.sort_keys();
        let mut python = expected.clone();
        python.sort_keys();
        assert_eq!(
            PyJson::Dict(sorted).to_json_string().unwrap(),
            PyJson::Dict(python).to_json_string().unwrap(),
            "{name}: {actual}"
        );
        let order = keys(&result.data);
        let python_order: &[&str] = if result.status == ResultStatus::Ok {
            &["matched_uids", "latest_internaldate"]
        } else {
            &["error_type", "error_msg", "attempts", "severity"]
        };
        assert_eq!(order, python_order[..order.len()], "{name}");

        let sleeps: Vec<Duration> = case
            .get("sleeps")
            .and_then(PyJson::as_list)
            .unwrap()
            .iter()
            .map(|delay| match delay {
                PyJson::Int(n) => Duration::from_secs(*n as u64),
                PyJson::Float(x) => Duration::from_secs_f64(*x),
                other => panic!("delay {other:?}"),
            })
            .collect();
        assert_eq!(env.sleeps(), sleeps, "{name}");
        let python: Vec<Vec<String>> = case
            .get("transcripts")
            .and_then(PyJson::as_list)
            .unwrap()
            .iter()
            .map(|lines| {
                lines
                    .as_list()
                    .unwrap()
                    .iter()
                    .map(|line| line.as_str().unwrap().to_owned())
                    .collect()
            })
            .collect();
        assert_eq!(transcripts, python, "{name}");
    }
}

#[tokio::test]
async fn a_silent_server_times_out() {
    let server = ScriptServer::start(vec![vec![Step::Stall]], false).await;
    let result = execute(&check(server.port, r#", "timeout": 0.2"#), &TestEnv::new())
        .await
        .expect("no exception");
    assert_eq!(
        PyJson::Dict(result.data).to_json_string().unwrap(),
        r#"{"error_type": "unexpected_error", "error_msg": "timed out", "attempts": 1}"#
    );
    server.transcripts().await;
}

#[tokio::test]
async fn a_refused_connection_is_an_unexpected_error() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let result = execute(&check(port, r#", "retries": 2"#), &TestEnv::new())
        .await
        .expect("no exception");
    assert_eq!(result.data["error_type"], PyJson::from("unexpected_error"));
    let message = result.data["error_msg"].as_str().unwrap().to_owned();
    assert!(
        message.starts_with("[Errno ") && message.ends_with("] Connection refused"),
        "{message}"
    );
    assert_eq!(result.data["attempts"], PyJson::Int(1));
}

fn login_select() -> Vec<Step> {
    vec![
        Step::Recv,
        tagged("OK [CAPABILITY IMAP4rev1 UIDPLUS] Logged in"),
        Step::Recv,
        send("* 3 EXISTS\r\n"),
        tagged("OK [READ-WRITE] SELECT completed"),
    ]
}

fn no_recent_and_logout() -> Vec<Step> {
    vec![
        Step::Recv,
        send("* SEARCH\r\n"),
        tagged("OK SEARCH completed"),
        Step::Recv,
        send("* BYE logging out\r\n"),
        tagged("OK LOGOUT completed"),
    ]
}

#[tokio::test]
async fn starttls_upgrades_and_asks_for_capabilities_again() {
    let mut script = vec![
        send("* OK [CAPABILITY IMAP4rev1 STARTTLS] ready\r\n"),
        Step::Recv,
        tagged("OK Begin TLS"),
        Step::StartTls,
        Step::Recv,
        send("* CAPABILITY IMAP4rev1 UIDPLUS\r\n"),
        tagged("OK done"),
    ];
    script.extend(login_select());
    script.extend(no_recent_and_logout());
    let server = ScriptServer::start(vec![script], false).await;
    let env = TestEnv::new();
    let extra = r#", "tls_mode": "starttls", "verify": false, "host": "localhost""#;
    let result = execute(&check(server.port, extra), &env).await.unwrap();
    assert_eq!(result.data["error_type"], PyJson::from("no_recent_message"));
    let transcript = untag(server.transcripts().await.remove(0));
    assert_eq!(
        transcript[..4],
        [
            "STARTTLS",
            "CAPABILITY",
            "LOGIN monitor \"secret\"",
            "SELECT INBOX"
        ]
        .map(str::to_owned)
    );
    let warnings = env.log.take();
    assert_eq!(warnings.len(), 1, "{warnings:?}");
    assert!(
        warnings[0]
            .1
            .contains("IMAP check for localhost runs with TLS certificate verification disabled"),
        "{warnings:?}"
    );
}

#[tokio::test]
async fn implicit_tls_without_verification_runs_the_session() {
    let mut script = vec![send(GREETING)];
    script.extend(login_select());
    script.extend(no_recent_and_logout());
    let server = ScriptServer::start(vec![script], true).await;
    let env = TestEnv::new();
    let extra = r#", "tls_mode": "implicit", "verify": false"#;
    let result = execute(&check(server.port, extra), &env).await.unwrap();
    assert_eq!(result.data["error_type"], PyJson::from("no_recent_message"));
    assert_eq!(untag(server.transcripts().await.remove(0)).len(), 4);
    assert_eq!(env.log.take().len(), 1);
}

#[tokio::test]
async fn certificate_failures_are_tls_errors_without_retry() {
    // Implicit TLS: the self-signed certificate is not trusted.
    let server = ScriptServer::start(vec![vec![]], true).await;
    let extra = r#", "tls_mode": "implicit", "retries": 2"#;
    let env = TestEnv::new();
    let result = execute(&check(server.port, extra), &env).await.unwrap();
    assert_eq!(result.data["error_type"], PyJson::from("tls_error"));
    assert_eq!(result.data["attempts"], PyJson::Int(1));
    assert!(env.sleeps().is_empty());
    assert!(env.log.take().is_empty(), "no warning with verification on");
    server.transcripts().await;

    // STARTTLS: the upgrade fails the same way.
    let script = vec![
        send("* OK [CAPABILITY IMAP4rev1 STARTTLS] ready\r\n"),
        Step::Recv,
        tagged("OK Begin TLS"),
        Step::StartTls,
    ];
    let server = ScriptServer::start(vec![script], false).await;
    let extra = r#", "tls_mode": "starttls", "retries": 2"#;
    let result = execute(&check(server.port, extra), &TestEnv::new())
        .await
        .unwrap();
    assert_eq!(result.data["error_type"], PyJson::from("tls_error"));
    assert_eq!(result.data["attempts"], PyJson::Int(1));
    server.transcripts().await;
}

#[tokio::test]
async fn configuration_problems_do_not_connect() {
    let env = TestEnv::new();
    let result = execute(&check(1, r#", "port": 0"#), &env).await.unwrap();
    assert_eq!(
        PyJson::Dict(result.data).to_json_string().unwrap(),
        r#"{"error_type": "configuration_error", "error_msg": "port must be an integer between 1 and 65535"}"#
    );
    let result = execute(&check(1, r#", "host": ["imap.example.com"]"#), &env)
        .await
        .unwrap();
    assert_eq!(
        result.data["error_msg"],
        PyJson::from(nyxmon_core::config::imap::UNSUPPORTED_HOST)
    );
    let outcome = execute(&check_data(loads("[1]").unwrap()), &env).await;
    assert_eq!(outcome.err(), Some(PyException::ATTRIBUTE_ERROR));
    let outcome = execute(&check(1, r#", "tls_mode": ["x"]"#), &env).await;
    assert_eq!(outcome.err(), Some(PyException::TYPE_ERROR));
    // `null` data is `{}` (PLAN.md D-11).
    let result = execute(&check_data(PyJson::Null), &env).await.unwrap();
    assert_eq!(result.data["error_msg"], PyJson::from("host is required"));
    // A numeric host fails like `"://" in host`.
    let result = execute(&check(1, r#", "host": 5"#), &env).await.unwrap();
    assert_eq!(
        result.data["error_msg"],
        PyJson::from("argument of type 'int' is not iterable")
    );
}

#[test]
fn imaplib_quoting_and_parsing_helpers() {
    assert_eq!(astring(&PyJson::from("INBOX")).unwrap(), b"INBOX");
    assert_eq!(astring(&PyJson::from("a b")).unwrap(), b"\"a b\"");
    assert_eq!(astring(&PyJson::from("\"q\"")).unwrap(), b"\"q\"");
    assert_eq!(astring(&PyJson::from("")).unwrap(), b"\"\"");
    assert_eq!(astring(&PyJson::from("a%b")).unwrap(), b"\"a%b\"");
    assert_eq!(
        astring(&PyJson::Int(5)),
        Err(ImapExc::Other(
            "expected string or bytes-like object, got 'int'".to_owned()
        ))
    );
    assert_eq!(quote(&PyJson::from("p\"w\\")).unwrap(), b"\"p\\\"w\\\\\"");
    assert_eq!(
        quote(&PyJson::Null),
        Err(ImapExc::Other(
            "'NoneType' object has no attribute 'replace'".to_owned()
        ))
    );
    assert_eq!(int2ap(4096), "BAAA");
    assert_eq!(int2ap(65535), "PPPP");
    assert_eq!(
        response_code(b"[CAPABILITY IMAP4rev1 X] hi ] there"),
        Some(("CAPABILITY".to_owned(), Some(b"IMAP4rev1 X] hi ".to_vec())))
    );
    assert_eq!(
        response_code(b"[READ-ONLY] x"),
        Some(("READ-ONLY".to_owned(), None))
    );
    assert_eq!(response_code(b"[ALERT x"), None);
    assert_eq!(response_code(b"[ALERT2]"), None);
    assert_eq!(literal_size(b"* 1 FETCH (BODY {12}"), Some(12));
    assert_eq!(literal_size(b"* 1 FETCH (BODY {x}"), None);
    assert_eq!(literal_size(b"{1}{2}"), Some(2));
    assert_eq!(paren_depth(b"(a \"(\" (b", 0), 2);
    assert_eq!(paren_depth(b"(a \"\\\"(\" b)", 1), 1);
    assert!(is_quoted(b"\"a\\\"b\""));
    assert!(!is_quoted(b"\"a\"b\""));
    assert_eq!(
        data_repr(&vec![
            None,
            Some(Datum::Literal(b"h".to_vec(), b"l".to_vec()))
        ]),
        "[None, (b'h', b'l')]"
    );
}

/// A clock that runs 30 times faster than real time.
struct FastClock {
    inner: TestEnv,
    start: std::time::Instant,
}

impl MailEnv for FastClock {
    fn now(&self) -> f64 {
        self.inner.now + self.start.elapsed().as_secs_f64() * 30.0
    }

    fn token(&self) -> String {
        self.inner.token()
    }

    fn message_id(&self, domain: &str) -> String {
        self.inner.message_id(domain)
    }

    async fn sleep(&self, delay: Duration) {
        self.inner.sleep(delay).await;
    }

    async fn local_hostname(&self) -> String {
        self.inner.local_hostname().await
    }

    fn log(&self) -> &dyn nyxmon_core::log::LogSink {
        self.inner.log()
    }
}

/// `search_recent` reads the clock after SEARCH returns: a slow SEARCH
/// ages a message past `max_age_minutes`.
#[tokio::test]
async fn the_cutoff_is_taken_after_the_search() {
    let env = FastClock {
        inner: TestEnv::new(),
        start: std::time::Instant::now(),
    };
    // 57 seconds old when the check starts; the SEARCH takes 0.3 s real
    // time, about 9 s on the fast clock.
    let date = "09-Oct-2025 08:52:24 +0000";
    let mut script = vec![send(GREETING)];
    script.extend(login_select());
    script.extend([
        Step::Recv,
        Step::Pause(Duration::from_millis(300)),
        send("* SEARCH 5\r\n"),
        tagged("OK SEARCH completed"),
        Step::Recv,
        send(&format!("* 1 FETCH (UID 5 INTERNALDATE \"{date}\")\r\n")),
        tagged("OK FETCH completed"),
        Step::Recv,
        tagged("OK LOGOUT completed"),
    ]);
    let server = ScriptServer::start(vec![script], false).await;
    let extra = r#", "max_age_minutes": 1, "delete_after_check": false"#;
    let result = execute(&check(server.port, extra), &env).await.unwrap();
    assert_eq!(result.data["error_type"], PyJson::from("no_recent_message"));
    server.transcripts().await;
}
