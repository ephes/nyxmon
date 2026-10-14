//! The HTTP executor against `fixtures/config_http.json` (`executor_cases`,
//! produced by the Python executor on a scripted transport) and fake-server
//! tests of the rules of PLAN.md 5.5 that the fixture cannot express.

use std::time::{Duration, Instant};

use nyxmon_core::pyjson::loads;

use crate::testutil::{Action, Executor, FakeServer, head, respond, run_fixture_case};

#[tokio::test(flavor = "multi_thread")]
async fn executor_cases_match_python() {
    let fixture = loads(include_str!("../../../../fixtures/config_http.json")).unwrap();
    let cases = fixture.get("executor_cases").unwrap().as_list().unwrap();
    assert!(cases.len() >= 30);
    for case in cases {
        run_fixture_case(case, Executor::Http, "http.test").await;
    }
}

use super::*;
use crate::ctx::Sleeper;
use crate::http_client::HttpSession;

fn check(url: &str, data: &str) -> Check {
    Check {
        check_id: 7,
        service_id: 1,
        name: "c".to_owned(),
        check_type: "http".to_owned(),
        url: url.to_owned(),
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

#[tokio::test]
async fn the_final_body_is_never_read() {
    // Headers announce a large body that never arrives.
    let mut step = vec![Action::Write(head(200, &[("Content-Length", "100000000")]))];
    step.push(Action::Hold);
    let server = FakeServer::start(vec![step]).await;
    let url = format!("http://127.0.0.1:{}/big", server.port());
    let started = Instant::now();
    let result = execute(&check(&url, r#"{"timeout": 5}"#), &ctx()).await;
    assert_eq!(result.status, ResultStatus::Ok, "{result:?}");
    assert!(started.elapsed() < Duration::from_secs(2));
}

#[tokio::test]
async fn a_stalled_redirect_body_times_out() {
    let mut step = vec![Action::Write(head(
        302,
        &[("Location", "/next"), ("Content-Length", "10")],
    ))];
    step.push(Action::Hold);
    let server = FakeServer::start(vec![step]).await;
    let url = format!("http://127.0.0.1:{}/", server.port());
    let result = execute(&check(&url, r#"{"timeout": 0.3}"#), &ctx()).await;
    assert_eq!(
        result.data.get("error_type").and_then(PyJson::as_str),
        Some("timeout")
    );
    assert_eq!(server.requests().len(), 1);
}

#[tokio::test]
async fn the_timeout_bounds_each_read_not_the_whole_request() {
    // Headers after 200 ms, then the body in pieces 200 ms apart: 1 s in
    // total with a 0.5 s timeout still succeeds on a redirect body.
    let mut step = vec![
        Action::Sleep(Duration::from_millis(200)),
        Action::Write(head(302, &[("Location", "/next"), ("Content-Length", "4")])),
    ];
    for _ in 0..4 {
        step.push(Action::Sleep(Duration::from_millis(200)));
        step.push(Action::Write(b"x".to_vec()));
    }
    let server = FakeServer::start(vec![step, respond(200, &[], b"")]).await;
    let url = format!("http://127.0.0.1:{}/", server.port());
    let result = execute(&check(&url, r#"{"timeout": 0.5}"#), &ctx()).await;
    assert_eq!(result.status, ResultStatus::Ok, "{result:?}");
    assert_eq!(server.requests().len(), 2);
}

#[tokio::test]
async fn a_refused_connection_is_a_connection_error() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let url = format!("http://127.0.0.1:{port}/");
    let result = execute(
        &check(&url, r#"{"retries": 1, "retry_delay": 0.5}"#),
        &ctx(),
    )
    .await;
    let keys: Vec<&str> = result.data.keys().map(String::as_str).collect();
    assert_eq!(keys, ["error_type", "error_msg", "attempt", "attempts"]);
    assert_eq!(result.data["error_type"].as_str(), Some("connection_error"));
    assert_eq!(
        result.data["error_msg"].as_str(),
        Some("All connection attempts failed")
    );
    assert_eq!(result.data["attempt"].as_i64(), Some(2));
}

#[tokio::test]
async fn url_errors_follow_the_httpx_classes() {
    for (url, error_type, retried) in [
        ("example.com/x", "request_error", true),
        ("ftp://example.com/x", "request_error", true),
        ("http://[::1/", "unexpected_error", false),
    ] {
        let (sleeper, delays) = Sleeper::recording();
        let ctx = BatchCtx {
            http: HttpSession::new(),
            sleeper,
        };
        let result = execute(&check(url, r#"{"retries": 1}"#), &ctx).await;
        assert_eq!(
            result.data["error_type"].as_str(),
            Some(error_type),
            "{url}"
        );
        assert_eq!(delays.lock().unwrap().len(), usize::from(retried), "{url}");
    }
}

#[tokio::test]
async fn an_invalid_location_fails_even_when_redirects_are_not_followed() {
    let server = FakeServer::start(vec![respond(301, &[("Location", "http://[::1")], b"")]).await;
    let url = format!("http://127.0.0.1:{}/", server.port());
    let data = r#"{"follow_redirects": false, "expected_status": 301, "expected_location": "x"}"#;
    let result = execute(&check(&url, data), &ctx()).await;
    assert_eq!(result.data["error_type"].as_str(), Some("request_error"));
}

#[tokio::test]
async fn retry_delays_wait_on_the_tokio_clock() {
    tokio::time::pause();
    let sleeper = Sleeper::real();
    let start = tokio::time::Instant::now();
    sleeper.sleep(2.5).await;
    let slept = start.elapsed();
    assert!(slept >= Duration::from_millis(2500) && slept < Duration::from_millis(2510));
    sleeper.sleep(0.0).await;
    sleeper.sleep(-1.0).await;
    sleeper.sleep(f64::NAN).await;
    assert_eq!(start.elapsed(), slept);
}
