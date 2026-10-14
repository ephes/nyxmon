//! The suppression fetch against `fixtures/suppression_fetch.json` (the
//! Python `_fetch_payload` on an httpx `MockTransport`), replayed against a
//! fake server, plus checks the mock transport cannot express.

use std::time::{Duration, Instant};

use nyxmon_core::pyjson::{PyJson, loads};
use nyxmon_core::suppression::suppression_request;

use super::*;
use crate::testutil::{Action, FakeServer, Step, head, respond, stall};

const HOST: &str = "maint.test";
const OTHER: &str = "other.test";

fn hex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
        .collect()
}

fn rehost(value: &PyJson, port: u16) -> PyJson {
    let mut text = value.to_json_string().unwrap();
    for name in [HOST, OTHER] {
        text = text.replace(name, &format!("{name}:{port}"));
    }
    loads(&text).unwrap()
}

fn step(value: &PyJson) -> Step {
    if value.get("timeout").is_some() {
        return stall();
    }
    let spec = value.get("respond").unwrap();
    let status = u16::try_from(spec.get("status").unwrap().as_i64().unwrap()).unwrap();
    let headers: Vec<(String, String)> = spec
        .get("headers")
        .unwrap()
        .as_list()
        .unwrap()
        .iter()
        .map(|pair| {
            let pair = pair.as_list().unwrap();
            (
                pair[0].as_str().unwrap().to_owned(),
                pair[1].as_str().unwrap().to_owned(),
            )
        })
        .collect();
    let headers: Vec<(&str, &str)> = headers
        .iter()
        .map(|(k, v)| (k.as_str(), v.as_str()))
        .collect();
    let body = match spec.get("pad").and_then(PyJson::as_list) {
        Some([pad, tail]) => format!(
            r#"{{"last_status": "running", "pad": "{}"}}{}"#,
            "x".repeat(usize::try_from(pad.as_i64().unwrap()).unwrap()),
            tail.as_str().unwrap()
        )
        .into_bytes(),
        _ => hex(spec.get("body_hex").unwrap().as_str().unwrap()),
    };
    if spec.get("stream").and_then(PyJson::as_bool) == Some(true) {
        // No Content-Length: the body ends with the connection.
        let mut actions = vec![Action::Write(head(status, &headers))];
        for chunk in body.chunks(16_384) {
            actions.push(Action::Write(chunk.to_vec()));
        }
        return actions;
    }
    respond(status, &headers, &body)
}

fn session(server: &FakeServer) -> HttpSession {
    HttpSession::new()
        .resolve(HOST, server.addr)
        .resolve(OTHER, server.addr)
}

#[tokio::test(flavor = "multi_thread")]
async fn fetch_cases_match_python() {
    let fixture = loads(include_str!("../../../../fixtures/suppression_fetch.json")).unwrap();
    let cases = fixture.get("cases").unwrap().as_list().unwrap();
    assert!(cases.len() >= 40);
    for case in cases {
        let server = FakeServer::start(Vec::new()).await;
        let steps = rehost(case.get("steps").unwrap(), server.port());
        server.set_script(steps.as_list().unwrap().iter().map(step).collect());
        let mut data = PyJson::dict();
        data.as_dict_mut().unwrap().insert(
            "notification_suppression".to_owned(),
            rehost(case.get("config").unwrap(), server.port()),
        );
        let request = suppression_request(&data).unwrap().unwrap();
        let label = format!("{:?}", case.get("config").unwrap());
        let mut payload = fetch_payload_with(&session(&server), &request).await;
        // The fixture records a padding string by its length.
        if let Some(map) = payload.as_mut().and_then(PyJson::as_dict_mut)
            && let Some(len) = map.get("pad").and_then(PyJson::as_str).map(str::len)
        {
            map.insert("pad".to_owned(), PyJson::Int(i64::try_from(len).unwrap()));
        }
        let outcome = case.get("outcome").unwrap();
        match outcome.get("payload") {
            Some(expected) => assert_eq!(payload.as_ref(), Some(expected), "{label}"),
            None => assert_eq!(payload, None, "{label}"),
        }
        let requests = server.requests();
        let expected = case.get("requests").unwrap().as_list().unwrap();
        assert_eq!(requests.len(), expected.len(), "{label}: {requests:?}");
        for (got, want) in requests.iter().zip(expected) {
            let host = got.header("host").unwrap_or_default();
            let host = host.rsplit_once(':').map_or(host, |(h, _)| h);
            assert_eq!(
                Some(host),
                want.get("host").and_then(PyJson::as_str),
                "{label}"
            );
            assert_eq!(
                Some(got.target.as_str()),
                want.get("path").and_then(PyJson::as_str),
                "{label}"
            );
            for (name, key) in [
                ("authorization", "authorization"),
                ("accept-encoding", "accept_encoding"),
                ("cookie", "cookie"),
            ] {
                assert_eq!(
                    got.header(name),
                    want.get(key).and_then(PyJson::as_str),
                    "{label}: {name}"
                );
            }
        }
    }
}

fn request(url: &str, timeout: f64) -> SuppressionRequest {
    SuppressionRequest {
        url: url.to_owned(),
        timeout,
        auth: None,
    }
}

#[tokio::test]
async fn redirect_bodies_are_never_read() {
    // A redirect whose body never ends: the fetch must not wait for it.
    let redirect = vec![
        Action::Write(head(
            302,
            &[("Location", "/final"), ("Content-Length", "100000000")],
        )),
        Action::Hold,
    ];
    let server = FakeServer::start(vec![
        redirect,
        respond(200, &[], br#"{"last_status": "running"}"#),
    ])
    .await;
    let started = Instant::now();
    let url = format!("http://127.0.0.1:{}/maintenance", server.port());
    let payload = fetch_payload_with(&HttpSession::new(), &request(&url, 5.0)).await;
    assert!(payload.is_some());
    assert!(started.elapsed() < Duration::from_secs(2));
}

#[tokio::test]
async fn the_timeout_bounds_each_read() {
    let server = FakeServer::start(vec![stall()]).await;
    let url = format!("http://127.0.0.1:{}/maintenance", server.port());
    let started = Instant::now();
    assert_eq!(
        fetch_payload_with(&HttpSession::new(), &request(&url, 0.3)).await,
        None
    );
    let elapsed = started.elapsed();
    assert!(elapsed >= Duration::from_millis(300) && elapsed < Duration::from_secs(2));
}

#[tokio::test]
async fn unusable_urls_and_timeouts_fail_open_without_a_request() {
    let server = FakeServer::start(Vec::new()).await;
    let port = server.port();
    for (url, timeout) in [
        (format!("ftp://127.0.0.1:{port}/x"), 3.0),
        (format!("127.0.0.1:{port}/x"), 3.0),
        ("http://".to_owned(), 3.0),
        (format!("http://127.0.0.1:{port}/x"), f64::NAN),
    ] {
        assert_eq!(
            fetch_payload_with(&HttpSession::new(), &request(&url, timeout)).await,
            None,
            "{url}"
        );
    }
    assert!(server.requests().is_empty());
}

#[tokio::test]
async fn a_connection_failure_fails_open() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let url = format!("http://127.0.0.1:{port}/x");
    assert_eq!(
        fetch_payload_with(&HttpSession::new(), &request(&url, 1.0)).await,
        None
    );
}
