//! A scripted HTTP/1.1 server on `127.0.0.1` for the executor tests.
//!
//! Every connection serves one request: the server reads the request head,
//! records it, and plays the next scripted step (writes, pauses, or holding
//! the connection open). An exhausted script answers `599`, like the
//! fixture generator's mock transport.

use std::collections::VecDeque;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

/// One action of a scripted response.
#[derive(Clone, Debug)]
pub(crate) enum Action {
    Write(Vec<u8>),
    Sleep(Duration),
    /// Keep the connection open without sending anything more.
    Hold,
}

pub(crate) type Step = Vec<Action>;

/// A request as the server received it.
#[derive(Clone, Debug)]
pub(crate) struct Recorded {
    pub target: String,
    pub headers: Vec<(String, String)>,
}

impl Recorded {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }
}

/// The status line and headers of a response (no `Content-Length` added).
pub(crate) fn head(status: u16, headers: &[(&str, &str)]) -> Vec<u8> {
    let mut out = format!("HTTP/1.1 {status} X\r\n");
    for (name, value) in headers {
        out.push_str(&format!("{name}: {value}\r\n"));
    }
    out.push_str("Connection: close\r\n\r\n");
    out.into_bytes()
}

/// A complete response; `Content-Length` is added unless given.
pub(crate) fn respond(status: u16, headers: &[(&str, &str)], body: &[u8]) -> Step {
    let length = body.len().to_string();
    let mut all: Vec<(&str, &str)> = headers.to_vec();
    if !headers
        .iter()
        .any(|(name, _)| name.eq_ignore_ascii_case("content-length"))
    {
        all.push(("Content-Length", &length));
    }
    let mut bytes = head(status, &all);
    bytes.extend_from_slice(body);
    vec![Action::Write(bytes)]
}

pub(crate) fn redirect(status: u16, location: &str) -> Step {
    respond(status, &[("Location", location)], b"moved")
}

/// Read the request but never answer (a read timeout on the client).
pub(crate) fn stall() -> Step {
    vec![Action::Hold]
}

/// Close the connection without a response.
pub(crate) fn disconnect() -> Step {
    Vec::new()
}

pub(crate) struct FakeServer {
    pub addr: SocketAddr,
    script: Arc<Mutex<VecDeque<Step>>>,
    requests: Arc<Mutex<Vec<Recorded>>>,
}

impl FakeServer {
    pub async fn start(steps: Vec<Step>) -> FakeServer {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let addr = listener.local_addr().expect("addr");
        let script = Arc::new(Mutex::new(VecDeque::from(steps)));
        let requests = Arc::new(Mutex::new(Vec::new()));
        let recorded = requests.clone();
        let shared = script.clone();
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                let script = shared.clone();
                let recorded = recorded.clone();
                tokio::spawn(serve(stream, script, recorded));
            }
        });
        FakeServer {
            addr,
            script,
            requests,
        }
    }

    /// Replace the script (for responses that name the server's port).
    pub fn set_script(&self, steps: Vec<Step>) {
        *self.script.lock().unwrap() = VecDeque::from(steps);
    }

    pub fn port(&self) -> u16 {
        self.addr.port()
    }

    pub fn requests(&self) -> Vec<Recorded> {
        self.requests.lock().unwrap().clone()
    }
}

async fn serve(
    mut stream: TcpStream,
    script: Arc<Mutex<VecDeque<Step>>>,
    recorded: Arc<Mutex<Vec<Recorded>>>,
) {
    let mut buf = Vec::new();
    let mut chunk = [0u8; 4096];
    while !buf.windows(4).any(|w| w == b"\r\n\r\n") {
        match stream.read(&mut chunk).await {
            Ok(0) | Err(_) => return,
            Ok(n) => buf.extend_from_slice(&chunk[..n]),
        }
    }
    let text = String::from_utf8_lossy(&buf).into_owned();
    let mut lines = text.split("\r\n");
    let target = lines
        .next()
        .and_then(|line| line.split(' ').nth(1))
        .unwrap_or_default()
        .to_owned();
    let headers = lines
        .take_while(|line| !line.is_empty())
        .filter_map(|line| line.split_once(':'))
        .map(|(k, v)| (k.trim().to_owned(), v.trim().to_owned()))
        .collect();
    recorded.lock().unwrap().push(Recorded { target, headers });
    let step = script
        .lock()
        .unwrap()
        .pop_front()
        .unwrap_or_else(|| respond(599, &[], b"script exhausted"));
    for action in step {
        match action {
            Action::Write(bytes) => {
                if stream.write_all(&bytes).await.is_err() {
                    return;
                }
                let _ = stream.flush().await;
            }
            Action::Sleep(duration) => tokio::time::sleep(duration).await,
            Action::Hold => {
                tokio::time::sleep(Duration::from_secs(60)).await;
                return;
            }
        }
    }
    let _ = stream.shutdown().await;
}

// ------------------------------------------------- fixture executor cases

use nyxmon_core::model::{Check, CheckResult};
use nyxmon_core::pyjson::{PyJson, loads};

use crate::ctx::{BatchCtx, Sleeper};
use crate::http_client::HttpSession;

/// Which executor a fixture case runs.
#[derive(Clone, Copy, Debug)]
pub(crate) enum Executor {
    Http,
    JsonMetrics,
}

fn field<'a>(value: &'a PyJson, key: &str) -> &'a PyJson {
    value
        .get(key)
        .unwrap_or_else(|| panic!("fixture field {key:?} missing"))
}

fn hex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).expect("hex"))
        .collect()
}

fn fixture_step(step: &PyJson) -> Step {
    if step.get("timeout").is_some() {
        return stall();
    }
    if step.get("disconnect").is_some() {
        return disconnect();
    }
    let respond_spec = field(step, "respond");
    let status = u16::try_from(field(respond_spec, "status").as_i64().expect("status")).unwrap();
    let headers: Vec<(String, String)> = field(respond_spec, "headers")
        .as_list()
        .expect("headers")
        .iter()
        .map(|pair| {
            let pair = pair.as_list().expect("pair");
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
    respond(
        status,
        &headers,
        &hex(field(respond_spec, "body_hex").as_str().unwrap()),
    )
}

/// A second host name the fixture cases redirect to (cross-origin).
const OTHER_HOST: &str = "other.test";

/// Add the fake server's port to every mention of the fixture host names
/// (`host`, e.g. `http.test`, and [`OTHER_HOST`]) in `value`.
fn rehost(value: &PyJson, host: &str, port: u16) -> PyJson {
    let mut text = value.to_json_string().expect("dumps");
    for name in [host, OTHER_HOST] {
        text = text.replace(name, &format!("{name}:{port}"));
    }
    loads(&text).expect("loads")
}

fn sorted(value: &PyJson) -> PyJson {
    match value {
        PyJson::Dict(map) => {
            let mut entries: Vec<(&String, &PyJson)> = map.iter().collect();
            entries.sort_by(|a, b| a.0.cmp(b.0));
            PyJson::Dict(
                entries
                    .into_iter()
                    .map(|(k, v)| (k.clone(), sorted(v)))
                    .collect(),
            )
        }
        PyJson::List(items) => PyJson::List(items.iter().map(sorted).collect()),
        other => other.clone(),
    }
}

const P2_ERROR_TYPES: [&str; 4] = [
    "timeout",
    "connection_error",
    "request_error",
    "unexpected_error",
];

fn keys(value: &PyJson) -> Vec<PyJson> {
    value
        .as_dict()
        .expect("dict")
        .keys()
        .map(|k| PyJson::from(k.as_str()))
        .collect()
}

/// Run one `executor_cases` entry of a `config_*.json` fixture against a
/// fake server and compare result, key order, requests and retry delays.
pub(crate) async fn run_fixture_case(case: &PyJson, executor: Executor, host: &str) {
    let server = FakeServer::start(Vec::new()).await;
    let steps = rehost(field(case, "steps"), host, server.port());
    server.set_script(
        steps
            .as_list()
            .expect("steps")
            .iter()
            .map(fixture_step)
            .collect(),
    );
    let data = rehost(field(case, "data"), host, server.port());
    let url = rehost(field(case, "url"), host, server.port());
    let (sleeper, delays) = Sleeper::recording();
    let ctx = BatchCtx {
        http: HttpSession::new()
            .resolve(host, server.addr)
            .resolve(OTHER_HOST, server.addr),
        sleeper,
    };
    let check = Check {
        check_id: 1,
        service_id: 1,
        name: "c".to_owned(),
        check_type: field(case, "check_type").as_str().unwrap().to_owned(),
        url: url.as_str().unwrap().to_owned(),
        check_interval: 300,
        next_check_time: 0,
        processing_started_at: 0,
        status: "idle".to_owned(),
        disabled: false,
        data,
        claim_started_at: 0,
    };
    let outcome: Result<CheckResult, nyxmon_core::config::PyException> = match executor {
        Executor::Http => Ok(crate::http::execute(&check, &ctx).await),
        Executor::JsonMetrics => crate::json_metrics::execute(&check, &ctx).await,
    };
    let expected = field(case, "outcome");
    let label = format!(
        "{:?} {:?} -> {outcome:?}",
        field(case, "url"),
        field(case, "data")
    );
    match outcome {
        Err(exc) => assert_eq!(
            expected.get("exception").and_then(PyJson::as_str),
            Some(exc.0),
            "{label}"
        ),
        Ok(result) => {
            assert!(
                expected.get("exception").is_none(),
                "{label}: got {result:?}"
            );
            assert_eq!(
                Some(result.status.as_str()),
                field(expected, "status").as_str(),
                "{label}"
            );
            let mut data = PyJson::Dict(result.data.clone());
            assert_eq!(
                PyJson::List(keys(&data)),
                *field(expected, "keys"),
                "{label}: key order"
            );
            if let Some(failure_keys) = expected.get("failure_keys") {
                let got: Vec<PyJson> = data
                    .get("failures")
                    .and_then(PyJson::as_list)
                    .unwrap()
                    .iter()
                    .map(|f| PyJson::List(keys(f)))
                    .collect();
                assert_eq!(
                    PyJson::List(got),
                    *failure_keys,
                    "{label}: failure key order"
                );
            }
            let map = data.as_dict_mut().unwrap();
            if let Some(duration) = map.get_mut("duration_ms") {
                assert!(duration.as_i64().is_some_and(|d| d >= 0), "{label}");
                *duration = PyJson::from("<duration>");
            }
            let error_type = map
                .get("error_type")
                .and_then(PyJson::as_str)
                .map(str::to_owned);
            let expected_p2 = field(expected, "data")
                .get("error_msg")
                .and_then(PyJson::as_str)
                == Some("<P2>");
            if expected_p2 || error_type.is_some_and(|t| P2_ERROR_TYPES.contains(&t.as_str())) {
                map.insert("error_msg".to_owned(), PyJson::from("<P2>"));
            }
            assert_eq!(sorted(&data), sorted(field(expected, "data")), "{label}");
        }
    }
    let requests = server.requests();
    let expected_requests = field(case, "requests").as_list().unwrap();
    assert_eq!(
        requests.len(),
        expected_requests.len(),
        "{label}: request count"
    );
    for (got, want) in requests.iter().zip(expected_requests) {
        assert_eq!(
            Some(got.target.as_str()),
            field(want, "path").as_str(),
            "{label}"
        );
        assert_eq!(
            got.header("authorization"),
            field(want, "authorization").as_str(),
            "{label}"
        );
        assert_eq!(
            got.header("cookie"),
            field(want, "cookie").as_str(),
            "{label}"
        );
    }
    let delays: Vec<PyJson> = delays
        .lock()
        .unwrap()
        .iter()
        .map(|d| PyJson::Float(*d))
        .collect();
    assert_eq!(
        PyJson::List(delays),
        *field(case, "delays"),
        "{label}: delays"
    );
}
