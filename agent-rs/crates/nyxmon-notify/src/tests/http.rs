//! Whole notify calls against the fake server.

use std::sync::Arc;
use std::time::Duration;

use nyxmon_core::env::{Env, notifier_env};
use nyxmon_core::log::RecordingSink;
use nyxmon_core::model::{CheckResult, ResultStatus};
use nyxmon_core::pyjson::{PyDict, PyJson};

use super::fake_server::{FakeServer, Reply};
use super::{check_of, field, int_field, list_field, load, result_of, str_field};
use crate::log::{Level, RecordingLog};
use crate::logging::LoggingNotifier;
use crate::telegram::{NotifierSettings, TelegramNotifier};
use crate::{Delivery, Notifier};

const TOKEN: &str = "123456:SECRET-bot_token~x";

fn telegram_fixture() -> PyJson {
    load(include_str!("../../../../fixtures/telegram.json"))
}

fn replies(spec: &PyJson, key: &str) -> Vec<Reply> {
    list_field(spec, key)
        .iter()
        .map(|reply| Reply::Respond {
            status: u16::try_from(int_field(reply, "status")).expect("status"),
            body: str_field(reply, "body").to_owned(),
            location: reply
                .get("location")
                .and_then(PyJson::as_str)
                .map(str::to_owned),
        })
        .collect()
}

/// A notifier for `vars`, whose Telegram endpoint and (when configured)
/// OpsGate submit endpoint are `server`.
fn notifier(
    vars: &[(String, String)],
    telegram_base: &str,
    opsgate_base: Option<&str>,
    now_micros: i64,
    nonce: &str,
) -> (TelegramNotifier, Arc<RecordingLog>) {
    let vars: Vec<(String, String)> = vars
        .iter()
        .map(|(k, v)| match (k.as_str(), opsgate_base) {
            ("OPSGATE_SUBMIT_BASE_URL", Some(base)) => (k.clone(), base.to_owned()),
            _ => (k.clone(), v.clone()),
        })
        .collect();
    let env = notifier_env(&Env::from_map(vars, Arc::new(RecordingSink::default())))
        .expect("notifier env");
    let log = Arc::new(RecordingLog::default());
    let nonce = nonce.to_owned();
    let settings = NotifierSettings {
        telegram_api_base: telegram_base.to_owned(),
        clock: Arc::new(move || now_micros),
        nonce: Arc::new(move || nonce.clone()),
        log: log.clone(),
        ..NotifierSettings::default()
    };
    (TelegramNotifier::new(&env, settings), log)
}

fn env_pairs(spec: &PyJson) -> Vec<(String, String)> {
    field(spec, "env")
        .as_dict()
        .expect("env dict")
        .iter()
        .map(|(k, v)| (k.clone(), v.as_str().expect("string").to_owned()))
        .collect()
}

fn level_name(level: Level) -> &'static str {
    level.as_str()
}

fn seconds(value: &PyJson, key: &str) -> Duration {
    match field(value, key) {
        PyJson::Float(f) => Duration::from_secs_f64(*f),
        other => panic!("timeout {key} is {other:?}"),
    }
}

#[tokio::test]
async fn http_scenarios_match_python() {
    let fixture = telegram_fixture();
    let now = int_field(&fixture, "now_micros");
    let nonce = str_field(&fixture, "nonce");
    assert_eq!(str_field(&fixture, "token"), TOKEN);
    let scenarios = list_field(&fixture, "http");
    assert!(scenarios.len() >= 30);
    for spec in scenarios {
        let label = str_field(spec, "label");
        let server = FakeServer::start(replies(spec, "telegram"), replies(spec, "opsgate")).await;
        let base = server.base();
        let (notifier, log) = notifier(&env_pairs(spec), &base, Some(&base), now, nonce);
        let check = check_of(field(spec, "check"));
        let result = result_of(field(spec, "result"));

        let outcome = notifier.notify_check_failed(&check, &result).await;
        match (spec.get("delivered"), spec.get("raised")) {
            (Some(expected), None) => {
                let expected = match expected {
                    PyJson::Bool(true) => Delivery::Delivered,
                    PyJson::Bool(false) => Delivery::Failed,
                    PyJson::Null => Delivery::Unknown,
                    other => panic!("{label}: delivered {other:?}"),
                };
                assert_eq!(outcome, Ok(expected), "{label}");
            }
            (None, Some(raised)) => {
                assert_eq!(
                    outcome.expect_err(label).class,
                    raised.as_str().expect("class"),
                    "{label}"
                );
            }
            other => panic!("{label}: outcome {other:?}"),
        }

        let requests = server.requests();
        let expected = list_field(spec, "requests");
        assert_eq!(requests.len(), expected.len(), "{label}: request count");
        for (got, want) in requests.iter().zip(expected) {
            assert_eq!(got.kind, str_field(want, "kind"), "{label}");
            assert_eq!(got.method, str_field(want, "method"), "{label}");
            assert_eq!(got.path, str_field(want, "path"), "{label}");
            assert_eq!(got.body, str_field(want, "body"), "{label}: body");
            let headers = field(want, "headers").as_dict().expect("headers");
            for name in ["accept", "authorization", "content-type", "user-agent"] {
                assert_eq!(
                    got.headers.get(name).map(String::as_str),
                    headers.get(name).and_then(PyJson::as_str),
                    "{label}: header {name}"
                );
            }
            assert!(!got.headers.contains_key("accept-encoding"), "{label}");
            let timeout = field(want, "timeout");
            let configured = if got.kind == "telegram" {
                notifier.telegram_timeout()
            } else {
                notifier.opsgate_timeout()
            };
            assert_eq!(configured, seconds(timeout, "connect"), "{label}");
            assert_eq!(configured, seconds(timeout, "read"), "{label}");
        }
        assert_eq!(server.unused(), 0, "{label}: unused replies");

        let logs: Vec<(String, String)> = log
            .take()
            .into_iter()
            .map(|(level, message)| (level_name(level).to_owned(), message))
            .collect();
        let expected_logs: Vec<(String, String)> = list_field(spec, "logs")
            .iter()
            .map(|line| {
                let line = line.as_list().expect("log line");
                (
                    line[0].as_str().expect("level").to_owned(),
                    line[1].as_str().expect("message").to_owned(),
                )
            })
            .collect();
        assert_eq!(logs, expected_logs, "{label}: logs");
        for (_, message) in &logs {
            assert!(!message.contains(TOKEN), "{label}: token in {message}");
        }
    }
}

fn basic() -> (nyxmon_core::model::Check, CheckResult) {
    let fixture = telegram_fixture();
    let spec = &list_field(&fixture, "http")[0];
    (
        check_of(field(spec, "check")),
        result_of(field(spec, "result")),
    )
}

fn telegram_vars() -> Vec<(String, String)> {
    vec![
        ("TELEGRAM_BOT_TOKEN".to_owned(), TOKEN.to_owned()),
        ("TELEGRAM_CHAT_ID".to_owned(), "-100123".to_owned()),
    ]
}

/// A local port with nothing listening.
async fn closed_port() -> String {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let addr = listener.local_addr().expect("addr");
    drop(listener);
    format!("http://{addr}")
}

fn assert_redacted(lines: &[(Level, String)]) {
    for (_, line) in lines {
        assert!(!line.contains(TOKEN), "token in {line}");
        assert!(!line.contains("SECRET"), "token in {line}");
    }
}

#[tokio::test]
async fn connection_failure_is_a_failed_send_without_the_token() {
    let base = closed_port().await;
    let (notifier, log) = notifier(&telegram_vars(), &base, None, 0, "0");
    let (check, result) = basic();
    assert_eq!(
        notifier.notify_check_failed(&check, &result).await,
        Ok(Delivery::Failed)
    );
    let lines = log.take();
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert_eq!(lines[0].0, Level::Error);
    assert!(
        lines[0]
            .1
            .starts_with("Failed to send Telegram notification: ConnectError: "),
        "{lines:?}"
    );
    assert_redacted(&lines);
}

#[tokio::test]
async fn stalled_telegram_times_out() {
    let server = FakeServer::start(vec![Reply::Stall], vec![]).await;
    let (notifier, log) = short_timeout_notifier(&server.base(), Duration::from_millis(200));
    assert_eq!(notifier.send("hi", true).await, Delivery::Failed);
    let lines = log.take();
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert!(
        lines[0]
            .1
            .starts_with("Failed to send Telegram notification: ReadTimeout: "),
        "{lines:?}"
    );
    assert_redacted(&lines);
}

fn short_timeout_notifier(base: &str, timeout: Duration) -> (TelegramNotifier, Arc<RecordingLog>) {
    let log = Arc::new(RecordingLog::default());
    let settings = NotifierSettings {
        telegram_api_base: base.to_owned(),
        telegram_timeout: timeout,
        log: log.clone(),
        ..NotifierSettings::default()
    };
    let env = notifier_env(&Env::from_map(
        telegram_vars(),
        Arc::new(RecordingSink::default()),
    ))
    .expect("env");
    (TelegramNotifier::new(&env, settings), log)
}

/// httpx's read timeout bounds each socket read, not the whole response: a
/// response that keeps arriving within the timeout is delivered even when it
/// takes several timeouts in total.
#[tokio::test]
async fn timeout_applies_per_read_not_per_response() {
    let server = FakeServer::start(
        vec![Reply::Dribble {
            gap: Duration::from_millis(120),
        }],
        vec![],
    )
    .await;
    let (notifier, log) = short_timeout_notifier(&server.base(), Duration::from_millis(400));
    // About ten pieces, 120 ms apart: well over the 400 ms timeout in total.
    assert_eq!(notifier.send("hi", true).await, Delivery::Delivered);
    assert_eq!(
        log.take(),
        vec![(Level::Info, "Telegram notification delivered".to_owned())]
    );
}

#[tokio::test]
async fn opsgate_response_echoing_the_token_is_redacted() {
    let server = FakeServer::start(
        vec![Reply::Respond {
            status: 200,
            body: "{}".to_owned(),
            location: None,
        }],
        vec![Reply::Respond {
            status: 500,
            body: format!("{{\"detail\": \"bad token {TOKEN} in /bot{TOKEN}/x\"}}"),
            location: None,
        }],
    )
    .await;
    let mut vars = telegram_vars();
    vars.extend([
        ("OPSGATE_SUBMIT_BASE_URL".to_owned(), server.base()),
        (
            "OPSGATE_SUBMIT_TOKEN".to_owned(),
            "opsgate-secret".to_owned(),
        ),
    ]);
    let (notifier, log) = notifier(&vars, &server.base(), None, 0, "0123456789abcdef");
    let (check, result) = basic();
    assert_eq!(
        notifier.notify_check_failed(&check, &result).await,
        Ok(Delivery::Delivered)
    );
    let lines = log.take();
    assert_eq!(
        lines[0].1,
        "OpsGate ticket submit returned status=500 for check_id=11 body={'detail': 'bad token \
         <redacted> in /bot<redacted>/x'}"
    );
    assert_redacted(&lines);
}

#[tokio::test]
async fn broken_success_body_is_a_failed_send() {
    let server = FakeServer::start(vec![Reply::ShortBody], vec![]).await;
    let (notifier, log) = notifier(&telegram_vars(), &server.base(), None, 0, "0");
    assert_eq!(notifier.send("hi", false).await, Delivery::Failed);
    let lines = log.take();
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert!(
        lines[0]
            .1
            .starts_with("Failed to send Telegram notification: "),
        "{lines:?}"
    );
    assert!(!lines[0].1.contains("delivered"));
    assert_redacted(&lines);
}

#[tokio::test]
async fn unreachable_opsgate_reports_a_ticket_error() {
    let opsgate = closed_port().await;
    let server = FakeServer::start(
        vec![Reply::Respond {
            status: 200,
            body: "{}".to_owned(),
            location: None,
        }],
        vec![],
    )
    .await;
    let mut vars = telegram_vars();
    vars.extend([
        ("OPSGATE_SUBMIT_BASE_URL".to_owned(), opsgate.clone()),
        (
            "OPSGATE_SUBMIT_TOKEN".to_owned(),
            "opsgate-secret".to_owned(),
        ),
        (
            "OPSGATE_APPROVAL_BASE_URL".to_owned(),
            "https://approve.example".to_owned(),
        ),
    ]);
    let (notifier, log) = notifier(&vars, &server.base(), None, 0, "0123456789abcdef");
    let (check, result) = basic();
    assert_eq!(
        notifier.notify_check_failed(&check, &result).await,
        Ok(Delivery::Delivered)
    );
    let lines = log.take();
    assert_eq!(lines.len(), 2, "{lines:?}");
    assert!(
        lines[0]
            .1
            .starts_with("OpsGate ticket submit failed for check_id=11: "),
        "{lines:?}"
    );
    assert_eq!(lines[1].1, "Telegram notification delivered");
    let sent = server.requests();
    assert!(
        sent[0]
            .body
            .contains("Ticket+creation+failed%3B+please+create+one+manually%5C."),
        "{}",
        sent[0].body
    );
    assert_redacted(&lines);
}

#[test]
fn debug_output_redacts_the_token() {
    let env = notifier_env(&Env::from_map(
        telegram_vars(),
        Arc::new(RecordingSink::default()),
    ))
    .expect("env");
    let notifier = TelegramNotifier::new(&env, NotifierSettings::default());
    let debug = format!("{notifier:?}");
    assert!(!debug.contains("SECRET"), "{debug}");
    assert!(debug.contains("<redacted>"));
}

#[tokio::test]
async fn logging_notifier_lines() {
    let log = Arc::new(RecordingLog::default());
    let (check, _) = basic();
    let mut data = PyDict::new();
    data.insert("error_type".to_owned(), PyJson::from("threshold"));
    data.insert("error_msg".to_owned(), PyJson::from("it's full"));
    let result = CheckResult::new(11, ResultStatus::Warning, data);

    let shadow = LoggingNotifier::with_log(true, log.clone());
    assert_eq!(
        shadow.notify_check_failed(&check, &result).await,
        Ok(Delivery::Delivered)
    );
    let plain = LoggingNotifier::with_log(false, log.clone());
    let mut unnamed = check.clone();
    unnamed.name.clear();
    let bare = CheckResult::new(11, ResultStatus::Error, PyDict::new());
    assert_eq!(
        plain.notify_check_failed(&unnamed, &bare).await,
        Ok(Delivery::Delivered)
    );
    assert_eq!(
        log.take(),
        vec![
            (
                Level::Error,
                "notification check_id=11 status=warning error_type=threshold".to_owned()
            ),
            (
                Level::Error,
                "Check failed: Check 11 (ID: 11), Result: Result(result_id=None, check_id=11 \
                 status=error, data={})"
                    .to_owned()
            ),
        ]
    );
    assert_eq!(
        shadow.line(&check, &bare),
        "notification check_id=11 status=error error_type="
    );
    assert_eq!(
        plain.line(&check, &result),
        "Check failed: Disk Pressure (ID: 11), Result: Result(result_id=None, check_id=11 \
         status=warning, data={'error_type': 'threshold', 'error_msg': \"it's full\"})"
    );
}
