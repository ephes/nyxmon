//! The DNS executor against `fixtures/config_dns.json` (`executor_cases`:
//! the Python executor with dnspython against a scripted DNS server), replayed
//! against the same script on a local fake DNS server (UDP and TCP on one
//! port).

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use nyxmon_core::model::Check;
use nyxmon_core::pyjson::{PyJson, loads};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, UdpSocket};

use super::*;

fn hex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
        .collect()
}

/// A scripted step: datagrams/segments to answer with, or no answer.
#[derive(Clone, Debug)]
enum Step {
    Reply {
        templates: Vec<Vec<u8>>,
        other_id: bool,
    },
    Drop,
    Close,
}

fn step_of(value: &PyJson) -> Step {
    if let Some(templates) = value.get("reply") {
        return Step::Reply {
            templates: templates
                .as_list()
                .unwrap()
                .iter()
                .map(|t| hex(t.as_str().unwrap()))
                .collect(),
            other_id: value.get("id").and_then(PyJson::as_str) == Some("other"),
        };
    }
    if value.get("close").is_some() {
        Step::Close
    } else {
        Step::Drop
    }
}

#[derive(Clone, Debug, PartialEq)]
struct Received {
    transport: &'static str,
    qname: String,
    qtype: u16,
    flags: u16,
    edns: i64,
    payload: i64,
}

struct FakeDns {
    port: u16,
    received: Arc<Mutex<Vec<Received>>>,
}

type Script = Arc<Mutex<VecDeque<Step>>>;

fn record(received: &Mutex<Vec<Received>>, wire: &[u8], transport: &'static str) -> u16 {
    let query = wire::parse_message(wire).expect("a query");
    let question = &query.questions[0];
    received.lock().unwrap().push(Received {
        transport,
        qname: question.name.to_text(),
        qtype: question.rtype,
        flags: query.flags,
        edns: if query.edns_flags.is_some() { 0 } else { -1 },
        payload: if query.edns_flags.is_some() {
            i64::from(query.edns_payload)
        } else {
            0
        },
    });
    query.id
}

fn answers(script: &Script, id: u16) -> Option<Vec<Vec<u8>>> {
    match script.lock().unwrap().pop_front().unwrap_or(Step::Drop) {
        Step::Reply {
            templates,
            other_id,
        } => Some(
            templates
                .into_iter()
                .map(|mut wire| {
                    if wire.len() >= 2 {
                        let reply_id = if other_id { id ^ 0xffff } else { id };
                        wire[..2].copy_from_slice(&reply_id.to_be_bytes());
                    }
                    wire
                })
                .collect(),
        ),
        Step::Drop => None,
        Step::Close => Some(Vec::new()),
    }
}

impl FakeDns {
    async fn start(steps: Vec<Step>) -> FakeDns {
        let (udp, tcp) = loop {
            let udp = UdpSocket::bind("127.0.0.1:0").await.unwrap();
            let port = udp.local_addr().unwrap().port();
            if let Ok(tcp) = TcpListener::bind(("127.0.0.1", port)).await {
                break (udp, tcp);
            }
        };
        let port = udp.local_addr().unwrap().port();
        let script: Script = Arc::new(Mutex::new(VecDeque::from(steps)));
        let received = Arc::new(Mutex::new(Vec::new()));
        {
            let script = script.clone();
            let received = received.clone();
            tokio::spawn(async move {
                let mut buf = vec![0u8; 65535];
                while let Ok((n, from)) = udp.recv_from(&mut buf).await {
                    let id = record(&received, &buf[..n], "udp");
                    for answer in answers(&script, id).unwrap_or_default() {
                        let _ = udp.send_to(&answer, from).await;
                    }
                }
            });
        }
        {
            let script = script.clone();
            let received = received.clone();
            tokio::spawn(async move {
                while let Ok((mut stream, _)) = tcp.accept().await {
                    let script = script.clone();
                    let received = received.clone();
                    tokio::spawn(async move {
                        let mut length = [0u8; 2];
                        if stream.read_exact(&mut length).await.is_err() {
                            return;
                        }
                        let mut body = vec![0u8; usize::from(u16::from_be_bytes(length))];
                        if stream.read_exact(&mut body).await.is_err() {
                            return;
                        }
                        let id = record(&received, &body, "tcp");
                        match answers(&script, id) {
                            None => tokio::time::sleep(Duration::from_secs(5)).await,
                            Some(list) => {
                                for answer in list {
                                    let mut framed =
                                        u16::try_from(answer.len()).unwrap().to_be_bytes().to_vec();
                                    framed.extend(answer);
                                    let _ = stream.write_all(&framed).await;
                                }
                            }
                        }
                    });
                }
            });
        }
        FakeDns { port, received }
    }
}

fn check(url: &str, data: PyJson) -> Check {
    Check {
        check_id: 1,
        service_id: 1,
        name: "c".to_owned(),
        check_type: "dns".to_owned(),
        url: url.to_owned(),
        check_interval: 300,
        next_check_time: 0,
        processing_started_at: 0,
        status: "idle".to_owned(),
        disabled: false,
        data,
        claim_started_at: 0,
    }
}

fn qtype_code(text: &str) -> u16 {
    nyxmon_core::config::dns::QueryType::parse(text)
        .map(|t| t.code())
        .unwrap()
}

#[tokio::test(flavor = "multi_thread")]
async fn executor_cases_match_python() {
    let fixture = loads(include_str!("../../../../fixtures/config_dns.json")).unwrap();
    let cases = fixture.get("executor_cases").unwrap().as_list().unwrap();
    assert!(cases.len() >= 50);
    let dir = std::env::temp_dir().join(format!("nyxmon-dns-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    for (index, case) in cases.iter().enumerate() {
        let steps: Vec<Step> = case
            .get("steps")
            .unwrap()
            .as_list()
            .unwrap()
            .iter()
            .map(step_of)
            .collect();
        let server = FakeDns::start(steps).await;
        let resolv_conf = dir.join(format!("resolv-{index}.conf"));
        match case.get("resolv_conf").and_then(PyJson::as_str) {
            Some(text) => std::fs::write(&resolv_conf, text).unwrap(),
            None => {
                let _ = std::fs::remove_file(&resolv_conf);
            }
        }
        let settings = DnsSettings {
            resolv_conf: resolv_conf.clone(),
            port: server.port,
        };
        let url = case.get("url").unwrap().as_str().unwrap();
        let data = case.get("data").unwrap().clone();
        let label = format!("case {index}: {url:?} {data:?}");
        let result = execute(&check(url, data), &settings).await;

        let outcome = case.get("outcome").unwrap();
        assert_eq!(
            Some(result.status.as_str()),
            outcome.get("status").and_then(PyJson::as_str),
            "{label}: {result:?}"
        );
        let keys: Vec<PyJson> = result
            .data
            .keys()
            .map(|k| PyJson::from(k.as_str()))
            .collect();
        assert_eq!(&PyJson::List(keys), outcome.get("keys").unwrap(), "{label}");
        let mut data = result.data.clone();
        if let Some(ms) = data.get_mut("query_time_ms") {
            assert!(ms.as_i64().is_some_and(|ms| ms >= 0));
            *ms = PyJson::from("<ms>");
        }
        if let Some(message) = data.get_mut("error_msg") {
            let text = message.as_str().unwrap().to_owned();
            if text.starts_with("All nameservers failed") {
                *message = PyJson::from("<P2>");
            } else if text == format!("cannot open {}", resolv_conf.display()) {
                *message = PyJson::from("cannot open /etc/resolv.conf");
            }
        }
        data.sort_keys();
        let mut expected_data = outcome.get("data").unwrap().as_dict().unwrap().clone();
        expected_data.sort_keys();
        assert_eq!(PyJson::Dict(data), PyJson::Dict(expected_data), "{label}");

        let received = server.received.lock().unwrap().clone();
        let expected = case.get("requests").unwrap().as_list().unwrap();
        assert_eq!(received.len(), expected.len(), "{label}: {received:?}");
        for (got, want) in received.iter().zip(expected) {
            let want = Received {
                transport: if want.get("transport").and_then(PyJson::as_str) == Some("tcp") {
                    "tcp"
                } else {
                    "udp"
                },
                qname: want.get("qname").unwrap().as_str().unwrap().to_owned(),
                qtype: qtype_code(want.get("qtype").unwrap().as_str().unwrap()),
                flags: u16::try_from(want.get("flags").unwrap().as_i64().unwrap()).unwrap(),
                edns: want.get("edns").unwrap().as_i64().unwrap(),
                payload: want.get("payload").unwrap().as_i64().unwrap(),
            };
            assert_eq!(got, &want, "{label}");
        }
    }
    let _ = std::fs::remove_dir_all(&dir);
}

fn data(text: &str) -> PyJson {
    loads(text).unwrap()
}

#[tokio::test]
async fn a_lost_query_times_out_after_the_lifetime() {
    let server = FakeDns::start(vec![Step::Drop, Step::Drop]).await;
    let dir = tempdir();
    let settings = DnsSettings {
        resolv_conf: write(&dir, "nameserver 127.0.0.1\n"),
        port: server.port,
    };
    let started = std::time::Instant::now();
    let result = execute(
        &check(
            "a.test",
            data(r#"{"expected_ips": ["192.0.2.1"], "dns_server": "127.0.0.1", "timeout": 0.3}"#),
        ),
        &settings,
    )
    .await;
    let elapsed = started.elapsed();
    assert_eq!(result.data["error_type"].as_str(), Some("timeout"));
    assert_eq!(
        result.data["error_msg"].as_str(),
        Some("DNS query timed out for a.test")
    );
    // One attempt for the whole lifetime, then the 0.1 s backoff ends it.
    assert!(elapsed >= Duration::from_millis(300), "{elapsed:?}");
    assert!(elapsed < Duration::from_millis(1500), "{elapsed:?}");
    assert_eq!(server.received.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn non_ascii_names_are_a_configuration_error() {
    // PLAN.md D-16: dnspython IDNA-encodes; the Rust agent fails closed.
    let dir = tempdir();
    let settings = DnsSettings {
        resolv_conf: write(&dir, "nameserver 127.0.0.1\n"),
        port: 9,
    };
    let result = execute(
        &check(
            "home.wersdörfer.de",
            data(r#"{"expected_ips": ["192.0.2.1"], "dns_server": "127.0.0.1"}"#),
        ),
        &settings,
    )
    .await;
    assert_eq!(
        result.data["error_type"].as_str(),
        Some("configuration_error")
    );
    assert_eq!(
        result.data["error_msg"].as_str(),
        Some("non-ASCII DNS names are not supported; use the IDNA (xn--) form")
    );
}

#[tokio::test]
async fn doh_nameservers_in_resolv_conf_are_a_configuration_error() {
    // PLAN.md D-16: dnspython would query the URL over DNS-over-HTTPS.
    let dir = tempdir();
    let settings = DnsSettings {
        resolv_conf: write(&dir, "nameserver https://dns.example/dns-query\n"),
        port: 9,
    };
    let result = execute(
        &check(
            "a.test",
            data(r#"{"expected_ips": ["192.0.2.1"], "dns_server": "127.0.0.1"}"#),
        ),
        &settings,
    )
    .await;
    assert_eq!(
        result.data["error_type"].as_str(),
        Some("configuration_error")
    );
    assert_eq!(
        result.data["error_msg"].as_str(),
        Some("DNS-over-HTTPS nameserver https://dns.example/dns-query is not supported")
    );
}

#[tokio::test]
async fn an_unreachable_nameserver_is_dropped_and_the_next_one_answers() {
    // A nameserver whose socket fails is dropped (OSError); the system list
    // goes on with the next one in the same round.
    let a_ok = {
        let name = wire::name_from_text("a.test").unwrap();
        let mut wire = Vec::new();
        wire.extend(0u16.to_be_bytes());
        wire.extend((wire::FLAG_QR | wire::FLAG_RD).to_be_bytes());
        for count in [1u16, 1, 0, 0] {
            wire.extend(count.to_be_bytes());
        }
        let mut q = Vec::new();
        name.write_wire(&mut q);
        wire.extend(&q);
        wire.extend([0, 1, 0, 1]);
        wire.extend(&q);
        wire.extend([0, 1, 0, 1, 0, 0, 1, 44, 0, 4, 192, 0, 2, 1]);
        wire
    };
    let server = FakeDns::start(vec![Step::Reply {
        templates: vec![a_ok],
        other_id: false,
    }])
    .await;
    let dir = tempdir();
    // An IPv6 nameserver with an IPv4 source address fails to bind.
    let settings = DnsSettings {
        resolv_conf: write(&dir, "nameserver ::1\nnameserver 127.0.0.1\n"),
        port: server.port,
    };
    let result = execute(
        &check(
            "a.test",
            data(r#"{"expected_ips": ["192.0.2.1"], "source_ip": "127.0.0.1", "timeout": 2}"#),
        ),
        &settings,
    )
    .await;
    assert_eq!(result.status.as_str(), "ok", "{result:?}");
    assert_eq!(result.data["dns_server"].as_str(), Some("system"));
}

struct TempDir(std::path::PathBuf);

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn tempdir() -> TempDir {
    static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("nyxmon-dns-unit-{}-{n}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    TempDir(dir)
}

fn write(dir: &TempDir, text: &str) -> std::path::PathBuf {
    let path = dir.0.join("resolv.conf");
    std::fs::write(&path, text).unwrap();
    path
}

/// A response for `a.test A` with `id` and one A record.
fn a_response(id: u16, octets: [u8; 4]) -> Vec<u8> {
    let mut wire = id.to_be_bytes().to_vec();
    wire.extend((wire::FLAG_QR | wire::FLAG_RD).to_be_bytes());
    for count in [1u16, 1, 0, 0] {
        wire.extend(count.to_be_bytes());
    }
    let mut name = Vec::new();
    wire::name_from_text("a.test")
        .unwrap()
        .write_wire(&mut name);
    wire.extend(&name);
    wire.extend([0, 1, 0, 1]);
    wire.extend(&name);
    wire.extend([0, 1, 0, 1, 0, 0, 1, 44, 0, 4]);
    wire.extend(octets);
    wire
}

#[tokio::test]
async fn responses_from_other_addresses_are_ignored() {
    // dnspython's `ignore_unexpected`: a matching response from another
    // port is skipped, the nameserver's own answer counts.
    let server = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let port = server.local_addr().unwrap().port();
    let spoofer = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    tokio::spawn(async move {
        let mut buf = vec![0u8; 512];
        let (n, from) = server.recv_from(&mut buf).await.unwrap();
        let id = wire::parse_message(&buf[..n]).unwrap().id;
        spoofer
            .send_to(&a_response(id, [192, 0, 2, 66]), from)
            .await
            .unwrap();
        tokio::time::sleep(Duration::from_millis(100)).await;
        server
            .send_to(&a_response(id, [192, 0, 2, 1]), from)
            .await
            .unwrap();
    });
    let dir = tempdir();
    let settings = DnsSettings {
        resolv_conf: write(&dir, "nameserver 127.0.0.1\n"),
        port,
    };
    let result = execute(
        &check(
            "a.test",
            data(r#"{"expected_ips": ["192.0.2.1"], "dns_server": "127.0.0.1", "timeout": 2}"#),
        ),
        &settings,
    )
    .await;
    assert_eq!(result.status.as_str(), "ok", "{result:?}");
    assert_eq!(
        result.data["rrset"],
        PyJson::List(vec![PyJson::from("192.0.2.1")])
    );
}
