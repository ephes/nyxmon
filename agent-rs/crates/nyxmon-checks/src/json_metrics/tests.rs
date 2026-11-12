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
    let mut outside_domain = 0;
    for case in cases {
        let data = case.get("data").unwrap();
        // Intentional difference: Python ran with a 10**300 s timeout; the
        // finite clock domain rejects it before any request.
        if matches!(data.get("timeout"), Some(PyJson::BigInt(_))) {
            outside_domain += 1;
            let result = execute(&check_with(data.clone()), &ctx()).await.unwrap();
            assert_eq!(
                result.data["error_type"].as_str(),
                Some("configuration_error"),
                "{data:?}"
            );
            continue;
        }
        run_fixture_case(case, Executor::JsonMetrics, "metrics.test").await;
    }
    assert_eq!(outside_domain, 1);
}

fn check(data: &str) -> Check {
    check_with(loads(data).unwrap())
}

fn check_with(data: PyJson) -> Check {
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
        data,
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
async fn clock_values_outside_the_finite_domain_are_configuration_errors() {
    // Python accepted these and then waited or retried without end; they
    // fail before any request or delay.
    let server = FakeServer::start(vec![]).await;
    let (sleeper, delays) = Sleeper::recording();
    let ctx = BatchCtx {
        http: HttpSession::new(),
        sleeper,
    };
    let huge = format!("1{}", "0".repeat(400));
    for (field, message) in [
        (
            format!(r#""timeout": {huge}"#),
            "timeout must be finite and at most 300 seconds",
        ),
        (
            r#""timeout": Infinity"#.to_owned(),
            "timeout must be finite and at most 300 seconds",
        ),
        (
            r#""timeout": NaN"#.to_owned(),
            "timeout must be finite and at most 300 seconds",
        ),
        (
            r#""timeout": 301"#.to_owned(),
            "timeout must be finite and at most 300 seconds",
        ),
        (
            r#""retry_delay": "inf""#.to_owned(),
            "retry_delay must be finite and at most 300 seconds",
        ),
        (
            r#""retry_delay": NaN"#.to_owned(),
            "retry_delay must be finite and at most 300 seconds",
        ),
        (
            r#""retries": 101"#.to_owned(),
            "retries must be at most 100",
        ),
        (
            format!(r#""retries": {huge}"#),
            "retries must be at most 100",
        ),
    ] {
        let data = format!(
            r#"{{"url": "http://127.0.0.1:{}/m", "checks": {RULES}, {field}}}"#,
            server.port()
        );
        let result = execute(&check(&data), &ctx).await.unwrap();
        assert_eq!(
            result.data["error_type"].as_str(),
            Some("configuration_error"),
            "{field}"
        );
        assert_eq!(result.data["error_msg"].as_str(), Some(message), "{field}");
    }
    assert!(server.requests().is_empty());
    assert!(delays.lock().unwrap().is_empty());
}

#[tokio::test]
async fn retries_stop_when_the_execution_budget_is_spent() {
    // A 1 s budget: after the first fast 503 the delay plus a whole attempt
    // (0.3 + 0.5 s) still fit, after the second (at about 0.3 s) they no
    // longer do. Without the budget all six attempts would run.
    let busy = || crate::testutil::respond(503, &[], b"");
    let server = FakeServer::start((0..6).map(|_| busy()).collect()).await;
    let ctx = BatchCtx {
        http: HttpSession::new(),
        sleeper: Sleeper::real(),
    };
    let data = format!(
        r#"{{"url": "http://127.0.0.1:{}/m", "checks": {RULES}, "timeout": 0.5,
             "retries": 5, "retry_delay": 0.3, "max_runtime_seconds": 1}}"#,
        server.port()
    );
    let started = std::time::Instant::now();
    let result = execute(&check(&data), &ctx).await.unwrap();
    assert_eq!(
        result.data["error_msg"].as_str(),
        Some("HTTP 503"),
        "{result:?}"
    );
    assert_eq!(server.requests().len(), 2);
    assert!(started.elapsed() < std::time::Duration::from_secs(1));
}

#[tokio::test]
async fn retries_within_the_derived_budget_are_unchanged() {
    // Without max_runtime_seconds the budget is the derived estimate, which
    // covers every configured retry.
    let busy = || crate::testutil::respond(503, &[], b"");
    let server = FakeServer::start((0..4).map(|_| busy()).collect()).await;
    let (sleeper, delays) = Sleeper::recording();
    let ctx = BatchCtx {
        http: HttpSession::new(),
        sleeper,
    };
    let data = format!(
        r#"{{"url": "http://127.0.0.1:{}/m", "checks": {RULES}, "retries": 3}}"#,
        server.port()
    );
    let result = execute(&check(&data), &ctx).await.unwrap();
    assert_eq!(
        result.data["error_msg"].as_str(),
        Some("HTTP 503"),
        "{result:?}"
    );
    assert_eq!(server.requests().len(), 4);
    assert_eq!(*delays.lock().unwrap(), vec![2.0, 2.0, 2.0]);
}

#[tokio::test]
async fn the_timeout_bounds_the_whole_attempt_including_the_body() {
    // The body drips in 100 ms apart; every read is fast, the attempt is not.
    let mut step = vec![Action::Write(head(200, &[("Content-Length", "10")]))];
    for _ in 0..10 {
        step.push(Action::Sleep(std::time::Duration::from_millis(100)));
        step.push(Action::Write(b" ".to_vec()));
    }
    let server = FakeServer::start(vec![step]).await;
    let data = format!(
        r#"{{"url": "http://127.0.0.1:{}/m", "checks": {RULES}, "timeout": 0.4, "retries": 0}}"#,
        server.port()
    );
    let started = std::time::Instant::now();
    let result = execute(&check(&data), &ctx()).await.unwrap();
    assert_eq!(
        result.data["error_type"].as_str(),
        Some("timeout"),
        "{result:?}"
    );
    assert!(started.elapsed() < std::time::Duration::from_millis(800));
}

#[tokio::test]
async fn cookies_never_cross_from_one_check_to_another_of_the_batch() {
    let server = FakeServer::start(vec![
        crate::testutil::respond(200, &[("Set-Cookie", "sid=secret; Path=/")], br#"{"a": 1}"#),
        crate::testutil::respond(200, &[], br#"{"a": 1}"#),
    ])
    .await;
    let data = format!(
        r#"{{"url": "http://127.0.0.1:{}/m", "checks": {RULES}}}"#,
        server.port()
    );
    let batch = ctx();
    for _ in 0..2 {
        let result = execute(&check(&data), &batch).await.unwrap();
        assert_eq!(result.status, ResultStatus::Ok, "{result:?}");
    }
    assert_eq!(server.requests()[1].header("cookie"), None);
}
