//! The json-metrics executor against `fixtures/config_json_metrics.json`
//! (`executor_cases`) and fake-server tests of the body rules.

use nyxmon_core::pyjson::loads;

use super::*;
use crate::ctx::Sleeper;
use crate::http_client::HttpSession;
use crate::testutil::{Action, Executor, FakeServer, head, run_fixture_case};

#[tokio::test(flavor = "multi_thread")]
async fn executor_cases_match_python() {
    let fixture = loads(include_str!(
        "../../../../fixtures/config_json_metrics.json"
    ))
    .unwrap();
    let cases = fixture.get("executor_cases").unwrap().as_list().unwrap();
    assert!(cases.len() >= 50);
    for case in cases {
        run_fixture_case(case, Executor::JsonMetrics, "metrics.test").await;
    }
}

fn check(data: &str) -> Check {
    Check {
        check_id: 3,
        service_id: 1,
        name: "m".to_owned(),
        check_type: "json-metrics".to_owned(),
        url: String::new(),
        check_interval: 300,
        next_check_time: 0,
        processing_started_at: 0,
        status: "idle".to_owned(),
        disabled: false,
        data: loads(data).unwrap(),
        claim_started_at: 0,
    }
}

fn ctx() -> BatchCtx {
    BatchCtx {
        http: HttpSession::new(),
        sleeper: Sleeper::recording().0,
    }
}

const RULES: &str = r#"[{"path": "$.a", "op": "<", "value": 10, "severity": "critical"}]"#;

#[tokio::test]
async fn a_body_without_declared_length_is_capped_while_streaming() {
    let mut step = vec![Action::Write(head(200, &[]))];
    for _ in 0..10 {
        step.push(Action::Write(vec![b' '; 50]));
    }
    let server = FakeServer::start(vec![step]).await;
    let data = format!(
        r#"{{"url": "http://127.0.0.1:{}/m", "checks": {RULES}, "max_body_bytes": 120}}"#,
        server.port()
    );
    let result = execute(&check(&data), &ctx()).await.unwrap();
    assert_eq!(result.data["error_type"].as_str(), Some("body_too_large"));
    assert_eq!(
        result.data["error_msg"].as_str(),
        Some("response body exceeds the 120 byte limit")
    );
}

#[tokio::test]
async fn an_error_response_body_is_not_read() {
    let step = vec![
        Action::Write(head(404, &[("Content-Length", "100000000")])),
        Action::Hold,
    ];
    let server = FakeServer::start(vec![step]).await;
    let data = format!(
        r#"{{"url": "http://127.0.0.1:{}/m", "checks": {RULES}, "timeout": 5}}"#,
        server.port()
    );
    let started = std::time::Instant::now();
    let result = execute(&check(&data), &ctx()).await.unwrap();
    assert_eq!(result.data["error_msg"].as_str(), Some("HTTP 404"));
    assert!(started.elapsed() < std::time::Duration::from_secs(2));
}

#[tokio::test]
async fn transport_errors_are_request_errors_and_retried() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    for url in [
        format!("http://127.0.0.1:{port}/m"),
        "example.com/m".to_owned(),
        "ftp://example.com/m".to_owned(),
    ] {
        let (sleeper, delays) = Sleeper::recording();
        let ctx = BatchCtx {
            http: HttpSession::new(),
            sleeper,
        };
        let data = format!(r#"{{"url": "{url}", "checks": {RULES}}}"#);
        let result = execute(&check(&data), &ctx).await.unwrap();
        let keys: Vec<&str> = result.data.keys().map(String::as_str).collect();
        assert_eq!(keys, ["error_type", "error_msg"]);
        assert_eq!(
            result.data["error_type"].as_str(),
            Some("request_error"),
            "{url}"
        );
        assert_eq!(*delays.lock().unwrap(), vec![2.0], "{url}");
    }
}

#[tokio::test]
async fn a_timeout_beyond_float_range_is_an_unexpected_error() {
    // httpcore raises OverflowError ("int too large to convert to float")
    // when it first uses the timeout: no request, no retry.
    let server = FakeServer::start(vec![]).await;
    let (sleeper, delays) = Sleeper::recording();
    let ctx = BatchCtx {
        http: HttpSession::new(),
        sleeper,
    };
    let data = format!(
        r#"{{"url": "http://127.0.0.1:{}/m", "checks": {RULES}, "timeout": 1{}}}"#,
        server.port(),
        "0".repeat(400)
    );
    let result = execute(&check(&data), &ctx).await.unwrap();
    assert_eq!(result.data["error_type"].as_str(), Some("unexpected_error"));
    assert!(server.requests().is_empty());
    assert!(delays.lock().unwrap().is_empty());
    // 10**300 still fits a float: effectively no timeout.
    let data = data.replace(&"0".repeat(400), &"0".repeat(300));
    server.set_script(vec![crate::testutil::respond(200, &[], br#"{"a": 1}"#)]);
    let result = execute(&check(&data), &ctx).await.unwrap();
    assert_eq!(result.status, ResultStatus::Ok, "{result:?}");
}
