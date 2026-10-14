//! SMTP executor against scripted fake servers: the wire dialogue is
//! compared line by line with what `smtplib` sends.

use std::time::Duration;

use nyxmon_core::model::{Check, ResultStatus};
use nyxmon_core::pyjson::{PyJson, loads};

use super::*;
use crate::fake_server::{ScriptServer, Step, TestEnv, send};

const GREETING: &str = "220 mail.example ESMTP\r\n";
const EHLO_REPLY: &str =
    "250-mail.example\r\n250-SIZE 1000000\r\n250-AUTH PLAIN LOGIN\r\n250 8BITMIME\r\n";

fn check(port: u16, extra: &str) -> Check {
    let text = format!(
        r#"{{"host": "127.0.0.1", "port": {port}, "tls": "none",
            "from_addr": "monitor@example.com", "to_addr": "alerts@example.com",
            "username": "monitor", "password": "secret",
            "retries": 2, "retry_delay": 5, "timeout": 5{extra}}}"#
    );
    Check {
        check_id: 7,
        service_id: 1,
        name: "smtp".to_owned(),
        check_type: "smtp".to_owned(),
        url: "smtp.example.com".to_owned(),
        check_interval: 300,
        next_check_time: 0,
        processing_started_at: 0,
        status: "processing".to_owned(),
        disabled: false,
        data: loads(&text).expect("valid check data"),
        claim_started_at: 0,
    }
}

const BODY: &str = "From: monitor@example.com\nTo: alerts@example.com\n\
Subject: [nyxmon] 2025-10-09T08:53:20Z a1b2c3\nMessage-ID: <1.2.3@example.com>\n\
Content-Type: text/plain; charset=\"utf-8\"\nContent-Transfer-Encoding: 7bit\n\
MIME-Version: 1.0\n\nNyxmon SMTP health check. Correlation token: a1b2c3. Safe to delete.";

fn body_size() -> usize {
    BODY.split('\n').map(|line| line.len() + 2).sum()
}

/// From EHLO through DATA for the default check (AUTH PLAIN).
fn delivery() -> Vec<Step> {
    vec![
        Step::Recv,
        send(EHLO_REPLY),
        Step::Recv,
        send("235 2.7.0 Authentication successful\r\n"),
        Step::Recv,
        send("250 2.1.0 Ok\r\n"),
        Step::Recv,
        send("250 2.1.5 Ok\r\n"),
        Step::Recv,
        send("354 End data with <CR><LF>.<CR><LF>\r\n"),
        Step::RecvData,
        send("250 2.0.0 Ok: queued\r\n"),
        Step::Recv,
        send("221 2.0.0 Bye\r\n"),
    ]
}

fn delivery_lines() -> Vec<String> {
    vec![
        "ehlo client.example".to_owned(),
        "AUTH PLAIN AG1vbml0b3IAc2VjcmV0".to_owned(),
        format!("mail from:<monitor@example.com> size={}", body_size()),
        "rcpt to:<alerts@example.com>".to_owned(),
        "data".to_owned(),
        format!("DATA:{BODY}"),
        "quit".to_owned(),
    ]
}

fn with_greeting(mut steps: Vec<Step>) -> Vec<Step> {
    steps.insert(0, send(GREETING));
    steps
}

fn data(result: &CheckResult) -> String {
    PyJson::Dict(result.data.clone())
        .to_json_string()
        .expect("serialisable")
}

#[tokio::test]
async fn delivery_matches_the_smtplib_dialogue() {
    let server = ScriptServer::start(vec![with_greeting(delivery())], false).await;
    let env = TestEnv::new();
    let result = execute(&check(server.port, ""), &env).await;
    assert_eq!(result.status, ResultStatus::Ok);
    assert_eq!(
        data(&result),
        r#"{"response_code": 250, "response_message": "accepted", "attempts": 1, "subject": "[nyxmon] 2025-10-09T08:53:20Z a1b2c3", "token": "a1b2c3", "from": "monitor@example.com", "to": "alerts@example.com"}"#
    );
    assert_eq!(server.transcripts().await, vec![delivery_lines()]);
    assert!(env.sleeps().is_empty());
    assert!(env.log.take().is_empty());
}

#[tokio::test]
async fn temporary_greeting_is_retried_after_the_delay() {
    let server = ScriptServer::start(
        vec![
            vec![send("421 4.3.2 Try again later\r\n")],
            with_greeting(delivery()),
        ],
        false,
    )
    .await;
    let env = TestEnv::new();
    let result = execute(&check(server.port, ""), &env).await;
    assert_eq!(result.status, ResultStatus::Ok);
    assert_eq!(result.data["attempts"], PyJson::Int(2));
    assert_eq!(env.sleeps(), vec![Duration::from_secs(5)]);
    assert_eq!(
        server.transcripts().await,
        vec![Vec::<String>::new(), delivery_lines()]
    );
}

#[tokio::test]
async fn temporary_failures_stop_after_the_retries() {
    let busy = || vec![send("451 4.7.1 Greylisted\r\n")];
    let server = ScriptServer::start(vec![busy(), busy()], false).await;
    let env = TestEnv::new();
    let result = execute(
        &check(server.port, r#", "retries": 1, "retry_delay": 0.5"#),
        &env,
    )
    .await;
    assert_eq!(
        data(&result),
        r#"{"error_type": "temporary_failure", "error_msg": "4.7.1 Greylisted", "attempts": 2, "smtp_code": 451}"#
    );
    assert_eq!(env.sleeps(), vec![Duration::from_millis(500)]);
    server.transcripts().await;
}

#[tokio::test]
async fn permanent_sender_refusal_resets_and_is_not_retried() {
    let script = with_greeting(vec![
        Step::Recv,
        send(EHLO_REPLY),
        Step::Recv,
        send("235 ok\r\n"),
        Step::Recv,
        send("550 5.7.1 Sender rejected\r\n"),
        Step::Recv,
        send("250 ok\r\n"),
        Step::Recv,
        send("221 bye\r\n"),
    ]);
    let server = ScriptServer::start(vec![script], false).await;
    let env = TestEnv::new();
    let result = execute(&check(server.port, ""), &env).await;
    assert_eq!(
        data(&result),
        r#"{"error_type": "smtp_error", "error_msg": "5.7.1 Sender rejected", "attempts": 1, "smtp_code": 550}"#
    );
    let lines = &server.transcripts().await[0];
    assert_eq!(lines[3..], ["rset".to_owned(), "quit".to_owned()]);
    assert!(env.sleeps().is_empty());
}

#[tokio::test]
async fn authentication_tries_each_mechanism_in_smtplib_order() {
    let script = with_greeting(vec![
        Step::Recv,
        send("250-mail.example\r\n250-AUTH LOGIN PLAIN\r\n250 AUTH=CRAM-MD5\r\n"),
        Step::Recv,
        send("334 PDQxODkuMTMwMDAwMDAwMEBleGFtcGxlPg==\r\n"),
        Step::Recv,
        send("535 5.7.8 nope\r\n"),
        Step::Recv,
        send("535 5.7.8 nope\r\n"),
        Step::Recv,
        send("334 UGFzc3dvcmQ6\r\n"),
        Step::Recv,
        send("535 5.7.8 Authentication failed\r\n"),
        Step::Recv,
        send("221 bye\r\n"),
    ]);
    let server = ScriptServer::start(vec![script], false).await;
    let env = TestEnv::new();
    let result = execute(&check(server.port, ""), &env).await;
    assert_eq!(
        data(&result),
        r#"{"error_type": "auth_error", "error_msg": "5.7.8 Authentication failed", "attempts": 1, "smtp_code": 535}"#
    );
    assert_eq!(
        server.transcripts().await[0][1..],
        [
            "AUTH CRAM-MD5",
            "bW9uaXRvciAxNmRmODY5NWIwYTQ1MjY2ZjI5MzZiYjMwMmI4MTViYg==",
            "AUTH PLAIN AG1vbml0b3IAc2VjcmV0",
            "AUTH LOGIN bW9uaXRvcg==",
            "c2VjcmV0",
            "quit",
        ]
        .map(str::to_owned)
    );
}

async fn single_error(script: Vec<Step>, extra: &str) -> (String, Vec<String>) {
    let server = ScriptServer::start(vec![script], false).await;
    let env = TestEnv::new();
    let result = execute(&check(server.port, extra), &env).await;
    assert_eq!(result.status, ResultStatus::Error);
    (data(&result), server.transcripts().await.remove(0))
}

#[tokio::test]
async fn missing_extensions_are_smtp_errors() {
    let ehlo_only = || {
        with_greeting(vec![
            Step::Recv,
            send("250 mail.example\r\n"),
            Step::Recv,
            send("221 bye\r\n"),
        ])
    };
    let (result, lines) = single_error(ehlo_only(), r#", "tls": "starttls""#).await;
    assert_eq!(
        result,
        r#"{"error_type": "smtp_error", "error_msg": "STARTTLS extension not supported by server.", "attempts": 1}"#
    );
    assert_eq!(lines, ["ehlo client.example", "quit"]);

    let (result, _) = single_error(ehlo_only(), "").await;
    assert!(result.contains("SMTP AUTH extension not supported by server."));

    let script = with_greeting(vec![
        Step::Recv,
        send("250-x\r\n250 AUTH XOAUTH2 plain\r\n"),
        Step::Recv,
        send("221 bye\r\n"),
    ]);
    let (result, _) = single_error(script, "").await;
    assert!(result.contains("No suitable authentication method found."));
}

#[tokio::test]
async fn partly_refused_recipients_report_the_first_refusal_after_delivery() {
    let script = with_greeting(vec![
        Step::Recv,
        send("250 mail.example\r\n"),
        Step::Recv,
        send("250 ok\r\n"),
        Step::Recv,
        send("550 5.1.1 unknown\r\n"),
        Step::Recv,
        send("452 4.2.2 full\r\n"),
        Step::Recv,
        send("250 ok\r\n"),
        Step::Recv,
        send("354 go\r\n"),
        Step::RecvData,
        send("250 queued\r\n"),
        Step::Recv,
        send("221 bye\r\n"),
    ]);
    let extra = r#", "username": null, "to_addr": "a@x.example, b@x.example, c@x.example""#;
    let (result, lines) = single_error(script, extra).await;
    assert_eq!(
        result,
        r#"{"error_type": "recipient_refused", "error_msg": "5.1.1 unknown", "attempts": 1, "smtp_code": 550}"#
    );
    assert_eq!(lines[1], "mail from:<monitor@example.com>");
    assert_eq!(
        lines[2..5],
        [
            "rcpt to:<a@x.example>",
            "rcpt to:<b@x.example>",
            "rcpt to:<c@x.example>"
        ]
        .map(str::to_owned)
    );
}

#[tokio::test]
async fn all_refused_recipients_report_the_python_dict() {
    let script = with_greeting(vec![
        Step::Recv,
        send("250 mail.example\r\n"),
        Step::Recv,
        send("250 ok\r\n"),
        Step::Recv,
        send("550 5.1.1 it's unknown\r\n"),
        Step::Recv,
        send("250 reset\r\n"),
        Step::Recv,
        send("221 bye\r\n"),
    ]);
    let (result, lines) = single_error(script, r#", "username": null"#).await;
    assert_eq!(
        result,
        r#"{"error_type": "smtp_error", "error_msg": "{'alerts@example.com': (550, b\"5.1.1 it's unknown\")}", "attempts": 1}"#
    );
    assert_eq!(lines[3..], ["rset", "quit"].map(str::to_owned));
}

#[tokio::test]
async fn quit_failures_after_data_keep_the_delivery() {
    let mut script = with_greeting(delivery());
    script.truncate(script.len() - 2);
    script.push(Step::Close);
    let server = ScriptServer::start(vec![script], false).await;
    let result = execute(&check(server.port, ""), &TestEnv::new()).await;
    assert_eq!(result.status, ResultStatus::Ok);
    server.transcripts().await;
}

#[tokio::test]
async fn data_rejection_is_a_response_error() {
    let mut script = with_greeting(delivery());
    script.truncate(10);
    script.extend([send("554 5.6.0 no\r\n"), Step::Recv, send("221 bye\r\n")]);
    let (result, lines) = single_error(script, "").await;
    assert_eq!(
        result,
        r#"{"error_type": "smtp_error", "error_msg": "5.6.0 no", "attempts": 1, "smtp_code": 554}"#
    );
    assert_eq!(lines.last().map(String::as_str), Some("quit"));
}

#[tokio::test]
async fn socket_failures_map_like_the_python_except_chain() {
    // A reply that never comes: the read timeout surfaces as a disconnect.
    let (result, _) = single_error(vec![Step::Stall], r#", "timeout": 0.2"#).await;
    assert_eq!(
        result,
        r#"{"error_type": "connection_error", "error_msg": "Connection unexpectedly closed: timed out", "attempts": 1}"#
    );
    // The server closes before the greeting.
    let (result, _) = single_error(vec![Step::Close], "").await;
    assert!(result.contains(r#""error_msg": "Connection unexpectedly closed""#));
    // An over-long reply line.
    let long = format!("220 {}\r\n", "x".repeat(9000));
    let (result, _) = single_error(vec![send(&long)], "").await;
    assert_eq!(
        result,
        r#"{"error_type": "smtp_error", "error_msg": "Line too long.", "attempts": 1, "smtp_code": 500}"#
    );
    // Nobody listening.
    let port = {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        listener.local_addr().unwrap().port()
    };
    let result = execute(&check(port, ""), &TestEnv::new()).await;
    let text = data(&result);
    assert!(
        text.contains(r#""error_type": "connection_error", "error_msg": "[Errno "#),
        "{text}"
    );
    assert!(text.contains("Connection refused"), "{text}");
}

#[tokio::test]
async fn implicit_tls_verifies_certificates_unless_disabled() {
    let server = ScriptServer::start(vec![vec![]], true).await;
    let env = TestEnv::new();
    let result = execute(&check(server.port, r#", "tls": "implicit""#), &env).await;
    assert_eq!(result.data["error_type"], PyJson::from("tls_error"));
    assert_eq!(result.data["attempts"], PyJson::Int(1));
    assert!(env.log.take().is_empty());
    server.transcripts().await;

    let server = ScriptServer::start(vec![with_greeting(delivery())], true).await;
    let env = TestEnv::new();
    let extra = r#", "tls": "implicit", "verify": false"#;
    let result = execute(&check(server.port, extra), &env).await;
    assert_eq!(result.status, ResultStatus::Ok, "{:?}", result.data);
    assert_eq!(server.transcripts().await, vec![delivery_lines()]);
    assert_eq!(
        env.log.take(),
        vec![(
            "nyxmon.adapters.runner.executors.tls".to_owned(),
            "SMTP check for 127.0.0.1 runs with TLS certificate verification disabled \
             (verify=false); credentials are exposed to man-in-the-middle attacks"
                .to_owned()
        )]
    );
}

#[tokio::test]
async fn starttls_upgrades_and_repeats_ehlo() {
    let mut script = with_greeting(vec![
        Step::Recv,
        send("250-mail.example\r\n250 STARTTLS\r\n"),
        Step::Recv,
        send("220 2.0.0 Ready to start TLS\r\n"),
        Step::StartTls,
    ]);
    script.extend(delivery());
    let server = ScriptServer::start(vec![script], false).await;
    let extra = r#", "tls": "starttls", "verify": false"#;
    let result = execute(&check(server.port, extra), &TestEnv::new()).await;
    assert_eq!(result.status, ResultStatus::Ok, "{:?}", result.data);
    let mut expected = vec!["ehlo client.example".to_owned(), "STARTTLS".to_owned()];
    expected.extend(delivery_lines());
    assert_eq!(server.transcripts().await, vec![expected]);

    let script = with_greeting(vec![
        Step::Recv,
        send("250-mail.example\r\n250 STARTTLS\r\n"),
        Step::Recv,
        send("454 4.7.0 TLS not available\r\n"),
        Step::Recv,
        send("221 bye\r\n"),
    ]);
    let server = ScriptServer::start(vec![script.clone(), script], false).await;
    let env = TestEnv::new();
    let extra = r#", "tls": "starttls", "retries": 1"#;
    let result = execute(&check(server.port, extra), &env).await;
    assert_eq!(
        data(&result),
        r#"{"error_type": "temporary_failure", "error_msg": "4.7.0 TLS not available", "attempts": 2, "smtp_code": 454}"#
    );
    assert_eq!(env.log.take(), vec![]);
    server.transcripts().await;
}

#[tokio::test]
async fn undecodable_auth_challenges_escape_as_unexpected_errors() {
    let script = with_greeting(vec![
        Step::Recv,
        send("250-mail.example\r\n250 AUTH LOGIN\r\n"),
        Step::Recv,
        send("334 YQ\r\n"),
        Step::Recv,
        send("221 bye\r\n"),
    ]);
    let (result, lines) = single_error(script, "").await;
    assert_eq!(
        result,
        r#"{"error_type": "unexpected_error", "error_msg": "Incorrect padding", "attempts": 1}"#
    );
    assert_eq!(
        lines[1..],
        ["AUTH LOGIN bW9uaXRvcg==", "quit"].map(str::to_owned)
    );
}

#[tokio::test]
async fn configuration_errors_do_not_connect() {
    let result = execute(&check(1, r#", "port": 0"#), &TestEnv::new()).await;
    assert_eq!(
        data(&result),
        r#"{"error_type": "configuration_error", "error_msg": "port must be an integer between 1 and 65535"}"#
    );
}

#[test]
fn ehlo_features_follow_smtplib_parsing() {
    let mut client = Client {
        conn: None,
        host: String::new(),
        local_hostname: String::new(),
        esmtp: IndexMap::new(),
        does_esmtp: false,
    };
    let reply = "mail.example\nSIZE 1000\nAUTH=LOGIN\nauth PLAIN  \n8BITMIME\n-bad\nX-FOO bar baz \n\u{e9}t\u{e9}";
    // The parsing part of `ehlo()`, fed directly.
    parse_ehlo_features(&mut client, reply.as_bytes());
    let features: Vec<(String, String)> = client.esmtp.into_iter().collect();
    assert_eq!(
        features,
        [
            ("size", "1000"),
            ("auth", " LOGIN PLAIN"),
            ("8bitmime", ""),
            ("x-foo", "bar baz"),
        ]
        .map(|(k, v)| (k.to_owned(), v.to_owned()))
    );
}

#[test]
fn helpers_follow_python() {
    assert_eq!(
        quote_periods(b".a\r\n..b\r\nc.\r\n."),
        b"..a\r\n...b\r\nc.\r\n.."
    );
    assert_eq!(decode_base64(b"UGFzc3dvcmQ6").unwrap(), b"Password:");
    assert_eq!(decode_base64(b"UGFz c3dv\ncmQ6").unwrap(), b"Password:");
    assert!(decode_base64(b"UGFzc").is_err());
    // CPython 3.13 `binascii.a2b_base64` corner cases.
    assert_eq!(decode_base64(b"YR==").unwrap(), b"a");
    assert_eq!(decode_base64(b"YQ==YQ").unwrap(), b"a\x06\x10");
    assert_eq!(decode_base64(b"=YQ==").unwrap(), b"a");
    let padding = SmtpExc::Other("Incorrect padding".to_owned());
    assert_eq!(decode_base64(b"YQ"), Err(padding.clone()));
    assert_eq!(decode_base64(b"YQ="), Err(padding.clone()));
    assert_eq!(decode_base64(b"Y=Q="), Err(padding));
    assert_eq!(
        decode_base64(b"YWJjZ"),
        Err(SmtpExc::Other(
            "Invalid base64-encoded string: number of data characters (5) cannot be 1 more than a multiple of 4"
                .to_owned()
        ))
    );
    assert_eq!(
        ascii_error("a\u{e4}b").as_deref(),
        Some(
            "'ascii' codec can't encode character '\\xe4' in position 1: ordinal not in range(128)"
        )
    );
    assert_eq!(
        ascii_error("ab\u{e4}\u{f6}").as_deref(),
        Some("'ascii' codec can't encode characters in position 2-3: ordinal not in range(128)")
    );
    assert_eq!(decode_response(b"\xff ok"), "b'\\xff ok'");
}

/// PLAN.md 1.5 D-14: an address outside the plain forms is a configuration
/// error before any connection (Python would send it).
#[tokio::test]
async fn unsupported_addresses_do_not_connect() {
    let result = execute(
        &check(1, r#", "to_addr": "Jürgen <j@x.example>""#),
        &TestEnv::new(),
    )
    .await;
    assert_eq!(
        data(&result),
        r#"{"error_type": "configuration_error", "error_msg": "to_addr is not supported by the Rust agent: use a plain ASCII address or a comma-separated list of them (user@example.com or Name <user@example.com>) that fits one header line"}"#
    );
}
