//! The TCP executor against local fake servers (`tests/unit/test_tcp_executor.py`
//! counterparts plus the result shapes of PLAN.md section 1.6).

use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::sync::{Arc, Mutex};

use nyxmon_core::pyjson::loads;
use rcgen::{CertificateParams, KeyPair, SanType};
use rustls::ServerConfig;
use rustls::pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::net::TcpListener;
use tokio::task::JoinHandle;
use tokio_rustls::TlsAcceptor;

use super::*;
use crate::tls::crypto_provider;

/// `notAfter` of the test certificates: 2030-01-01T00:00:00Z.
const NOT_AFTER: i64 = 1_893_456_000;
const DAY: f64 = 86_400.0;

fn check(data: &str, url: &str) -> Check {
    Check {
        check_id: 7,
        service_id: 1,
        name: "tcp".to_owned(),
        check_type: "tcp".to_owned(),
        url: url.to_owned(),
        check_interval: 60,
        next_check_time: 0,
        processing_started_at: 0,
        status: "processing".to_owned(),
        disabled: false,
        data: loads(data).expect("valid check data"),
        claim_started_at: 0,
    }
}

/// Records the requested sleeps and runs `hook` on each.
#[derive(Clone, Default)]
struct Sleeps {
    calls: Arc<Mutex<Vec<Duration>>>,
}

impl Sleeps {
    fn sleep(&self) -> Sleep {
        let calls = self.calls.clone();
        Arc::new(move |duration| {
            calls.lock().unwrap().push(duration);
            Box::pin(async {})
        })
    }

    fn calls(&self) -> Vec<Duration> {
        self.calls.lock().unwrap().clone()
    }
}

/// A clock `days` days before the certificates' `notAfter`.
fn clock_days_before_expiry(days: f64) -> WallClock {
    Arc::new(move || NOT_AFTER as f64 - days * DAY)
}

fn executor(sleeps: &Sleeps) -> TcpExecutor {
    TcpExecutor::default()
        .with_sleep(sleeps.sleep())
        .with_wall_clock(clock_days_before_expiry(10.5))
}

fn keys(data: &PyDict) -> Vec<&str> {
    data.keys().map(String::as_str).collect()
}

fn int(data: &PyDict, key: &str) -> i64 {
    data.get(key)
        .and_then(PyJson::as_i64)
        .unwrap_or_else(|| panic!("{key} missing in {data:?}"))
}

fn text<'a>(data: &'a PyDict, key: &str) -> &'a str {
    data.get(key)
        .and_then(PyJson::as_str)
        .unwrap_or_else(|| panic!("{key} missing in {data:?}"))
}

struct Pki {
    server: Arc<ServerConfig>,
    ca: CertificateDer<'static>,
}

fn trusting(pki: &Pki) -> Arc<RootCertStore> {
    let mut roots = RootCertStore::empty();
    roots.add(pki.ca.clone()).unwrap();
    Arc::new(roots)
}

/// A self-signed CA and a leaf for `localhost`/`127.0.0.1` that expires at
/// [`NOT_AFTER`].
fn pki() -> Pki {
    pki_for(vec![
        SanType::DnsName("localhost".try_into().unwrap()),
        SanType::IpAddress(IpAddr::V4(Ipv4Addr::LOCALHOST)),
    ])
}

/// A self-signed CA and a leaf with the given SANs.
fn pki_for(subject_alt_names: Vec<SanType>) -> Pki {
    let mut ca_params = CertificateParams::new(Vec::<String>::new()).unwrap();
    ca_params.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
    let ca = rcgen::CertifiedIssuer::self_signed(ca_params, KeyPair::generate().unwrap()).unwrap();
    let leaf_key = KeyPair::generate().unwrap();
    let mut params = CertificateParams::new(Vec::<String>::new()).unwrap();
    params.subject_alt_names = subject_alt_names;
    params.not_after = rcgen::date_time_ymd(2030, 1, 1);
    let leaf = params.signed_by(&leaf_key, &ca).unwrap();
    let key = PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(leaf_key.serialize_der()));
    let server = ServerConfig::builder_with_provider(crypto_provider())
        .with_safe_default_protocol_versions()
        .unwrap()
        .with_no_client_auth()
        .with_single_cert(vec![leaf.der().clone()], key)
        .unwrap();
    Pki {
        server: Arc::new(server),
        ca: ca.der().clone(),
    }
}

/// What the TLS side of a fake server does after the handshake.
#[derive(Clone, Copy)]
enum AfterHandshake {
    /// Answer the client's `close_notify` with one.
    CloseNotify,
    /// Close the TCP connection without `close_notify`.
    DropConnection,
    /// Send a greeting first, then answer `close_notify`.
    SendData,
}

#[derive(Default)]
struct Observed {
    sni: Mutex<Vec<Option<String>>>,
    lines: Mutex<Vec<Vec<u8>>>,
}

async fn serve_tls(
    socket: TcpStream,
    acceptor: TlsAcceptor,
    after: AfterHandshake,
    observed: &Observed,
) {
    let Ok(mut tls) = acceptor.accept(socket).await else {
        return;
    };
    observed
        .sni
        .lock()
        .unwrap()
        .push(tls.get_ref().1.server_name().map(str::to_owned));
    if matches!(after, AfterHandshake::SendData) {
        let _ = tls.write_all(b"* OK hello\r\n").await;
        let _ = tls.flush().await;
    }
    let mut buffer = [0u8; 64];
    while matches!(tls.read(&mut buffer).await, Ok(n) if n > 0) {}
    match after {
        AfterHandshake::DropConnection => drop(tls.into_inner().0),
        _ => {
            let _ = tls.shutdown().await;
        }
    }
}

/// An implicit-TLS server for any number of connections.
async fn tls_server(
    pki: &Pki,
    after: AfterHandshake,
) -> (SocketAddr, Arc<Observed>, JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let acceptor = TlsAcceptor::from(pki.server.clone());
    let observed = Arc::new(Observed::default());
    let seen = observed.clone();
    let task = tokio::spawn(async move {
        while let Ok((socket, _)) = listener.accept().await {
            serve_tls(socket, acceptor.clone(), after, &seen).await;
        }
    });
    (addr, observed, task)
}

type Responder = fn(&[u8]) -> (&'static [u8], bool);

/// A greet-first STARTTLS server: replies per line via `respond` and
/// upgrades when it says so (`_start_dialogue_server`).
async fn dialogue_server(
    pki: &Pki,
    greeting: &'static [u8],
    respond: Responder,
) -> (SocketAddr, Arc<Observed>, JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let acceptor = TlsAcceptor::from(pki.server.clone());
    let observed = Arc::new(Observed::default());
    let seen = observed.clone();
    let task = tokio::spawn(async move {
        while let Ok((socket, _)) = listener.accept().await {
            let mut reader = BufReader::new(socket);
            if reader.get_mut().write_all(greeting).await.is_err() {
                continue;
            }
            let upgrade = loop {
                let mut line = Vec::new();
                match reader.read_until(b'\n', &mut line).await {
                    Ok(0) | Err(_) => break false,
                    Ok(_) => {}
                }
                seen.lines.lock().unwrap().push(line.clone());
                let (reply, upgrade) = respond(&line);
                if reader.get_mut().write_all(reply).await.is_err() {
                    break false;
                }
                if upgrade {
                    break true;
                }
            };
            if upgrade {
                serve_tls(
                    reader.into_inner(),
                    acceptor.clone(),
                    AfterHandshake::CloseNotify,
                    &seen,
                )
                .await;
            }
        }
    });
    (addr, observed, task)
}

fn smtp(line: &[u8]) -> (&'static [u8], bool) {
    match line.to_ascii_uppercase().as_slice() {
        l if l.starts_with(b"EHLO") => (
            b"250-mail.test\r\n250-PIPELINING\r\n250 STARTTLS\r\n",
            false,
        ),
        b"STARTTLS\r\n" => (b"220 2.0.0 Ready to start TLS\r\n", true),
        _ => (b"500 5.5.2 unknown command\r\n", false),
    }
}

fn smtp_refusing(line: &[u8]) -> (&'static [u8], bool) {
    match smtp(line) {
        (_, true) => (b"454 4.7.0 TLS not available\r\n", false),
        other => other,
    }
}

fn imap(line: &[u8]) -> (&'static [u8], bool) {
    if line == b"a1 STARTTLS\r\n" {
        (b"* CAPABILITY IMAP4rev1\r\na1 OK Begin TLS now\r\n", true)
    } else {
        (b"* BAD Error in IMAP command\r\n", false)
    }
}

fn sieve(line: &[u8]) -> (&'static [u8], bool) {
    if line == b"STARTTLS\r\n" {
        (b"OK \"Begin TLS negotiation now.\"\r\n", true)
    } else {
        (b"NO \"Unknown command\"\r\n", false)
    }
}

fn generic(line: &[u8]) -> (&'static [u8], bool) {
    if line == b"STARTTLS\r\n" {
        (b"220 go ahead\r\n", true)
    } else {
        (b"500 unknown\r\n", false)
    }
}

const SMTP_GREETING: &[u8] = b"220-mail.test ESMTP\r\n220 mail.test ready\r\n";
const IMAP_GREETING: &[u8] = b"* OK [CAPABILITY IMAP4rev1 STARTTLS] mail.test ready\r\n";
const SIEVE_GREETING: &[u8] =
    b"\"IMPLEMENTATION\" \"Pigeonhole Sieve\"\r\n\"STARTTLS\"\r\nOK \"mail.test ready.\"\r\n";

/// A port with nothing listening.
fn closed_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

#[tokio::test]
async fn plain_tcp_success_has_the_python_shape() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let sleeps = Sleeps::default();
    let result = executor(&sleeps)
        .execute(&check(&format!(r#"{{"port": {port}}}"#), "127.0.0.1"))
        .await
        .unwrap();
    assert_eq!(result.status, ResultStatus::Ok);
    assert_eq!(result.check_id, 7);
    assert_eq!(
        keys(&result.data),
        [
            "host",
            "port",
            "tls_mode",
            "connect_time_ms",
            "attempt",
            "attempts"
        ]
    );
    assert_eq!(text(&result.data, "host"), "127.0.0.1");
    assert_eq!(int(&result.data, "port"), i64::from(port));
    assert_eq!(text(&result.data, "tls_mode"), "none");
    assert!(int(&result.data, "connect_time_ms") >= 0);
    assert_eq!(int(&result.data, "attempt"), 1);
    assert_eq!(int(&result.data, "attempts"), 2);
}

#[tokio::test]
async fn host_comes_from_the_url() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let result = executor(&Sleeps::default())
        .execute(&check(
            &format!(r#"{{"port": {port}}}"#),
            "https://LOCALHOST:8443/status",
        ))
        .await
        .unwrap();
    assert_eq!(result.status, ResultStatus::Ok, "{:?}", result.data);
    assert_eq!(text(&result.data, "host"), "localhost");
}

#[tokio::test]
async fn refused_connections_are_retried_then_reported() {
    let port = closed_port();
    let sleeps = Sleeps::default();
    let result = executor(&sleeps)
        .execute(&check(
            &format!(r#"{{"port": {port}, "retries": 2, "retry_delay": 0.5}}"#),
            "127.0.0.1",
        ))
        .await
        .unwrap();
    assert_eq!(result.status, ResultStatus::Error);
    assert_eq!(
        keys(&result.data),
        [
            "error_type",
            "error_msg",
            "host",
            "port",
            "tls_mode",
            "attempt",
            "attempts"
        ]
    );
    assert_eq!(text(&result.data, "error_type"), "connection_error");
    assert_eq!(
        text(&result.data, "error_msg"),
        format!("Connection to 127.0.0.1:{port} failed: All connection attempts failed")
    );
    assert_eq!(int(&result.data, "attempt"), 3);
    assert_eq!(int(&result.data, "attempts"), 3);
    assert_eq!(sleeps.calls(), [Duration::from_millis(500); 2]);
}

#[tokio::test]
async fn a_transient_failure_succeeds_on_the_next_attempt() {
    let port = closed_port();
    let listener: Arc<Mutex<Option<std::net::TcpListener>>> = Arc::default();
    let slot = listener.clone();
    let sleep: Sleep = Arc::new(move |_| {
        // The server comes up while the executor waits for the retry.
        *slot.lock().unwrap() = Some(std::net::TcpListener::bind(("127.0.0.1", port)).unwrap());
        Box::pin(async {})
    });
    let result = TcpExecutor::default()
        .with_sleep(sleep)
        .execute(&check(
            &format!(r#"{{"port": {port}, "retries": 1, "retry_delay": 0.1}}"#),
            "127.0.0.1",
        ))
        .await
        .unwrap();
    assert_eq!(result.status, ResultStatus::Ok, "{:?}", result.data);
    assert_eq!(int(&result.data, "attempt"), 2);
}

fn config(data: &str) -> TcpConfig {
    match TcpConfig::from_data(&loads(data).unwrap()) {
        ConfigOutcome::Ok(config) => config,
        other => panic!("{other:?}"),
    }
}

#[tokio::test(start_paused = true)]
async fn connect_timeout_and_resolver_errors_are_classified() {
    let config = config(r#"{"port": 443, "connect_timeout": 2.5}"#);
    let host = PyJson::Str("db.example".to_owned());
    let started = Instant::now();
    let outcome = bounded_connect(&host, &config, std::future::pending()).await;
    assert_eq!(started.elapsed(), Duration::from_millis(2500));
    assert_eq!(
        outcome.unwrap_err(),
        AttemptError::Check(TcpCheckError::new(
            "connect_timeout",
            "Connection to db.example:443 timed out".to_owned(),
            true
        ))
    );

    let gai =
        std::io::Error::other("failed to lookup address information: Name or service not known");
    let expected = format!(
        "Connection to db.example:443 failed: {}",
        os_error_str(&gai)
    );
    let outcome = bounded_connect(&host, &config, async {
        Err(ConnectFailure::Connect(ConnectError::Resolve(gai)))
    })
    .await;
    assert_eq!(
        outcome.unwrap_err(),
        AttemptError::Check(TcpCheckError::new("connection_error", expected, true))
    );

    let outcome = bounded_connect(&PyJson::Int(5), &config, async {
        Err(ConnectFailure::Connect(ConnectError::AllFailed))
    })
    .await;
    assert_eq!(
        outcome.unwrap_err(),
        AttemptError::Check(TcpCheckError::new(
            "connection_error",
            "Connection to 5:443 failed: All connection attempts failed".to_owned(),
            true
        ))
    );
}

#[tokio::test]
async fn a_host_that_is_not_idna_encodable_is_unexpected() {
    let result = executor(&Sleeps::default())
        .execute(&check(
            r#"{"port": 443, "host": "☃.net", "retries": 3}"#,
            "",
        ))
        .await
        .unwrap();
    assert_eq!(text(&result.data, "error_type"), "unexpected_error");
    assert_eq!(
        keys(&result.data),
        [
            "error_type",
            "error_msg",
            "host",
            "port",
            "tls_mode",
            "attempt",
            "attempts"
        ]
    );
    assert_eq!(int(&result.data, "attempt"), 1);
}

#[tokio::test]
async fn implicit_tls_reports_handshake_time_and_certificate_days() {
    let pki = pki();
    let (addr, observed, _server) = tls_server(&pki, AfterHandshake::CloseNotify).await;
    let data = format!(
        r#"{{"port": {}, "tls_mode": "implicit", "verify": false, "check_cert_expiry": true, "min_cert_days": 10}}"#,
        addr.port()
    );
    let result = executor(&Sleeps::default())
        .execute(&check(&data, "127.0.0.1"))
        .await
        .unwrap();
    assert_eq!(result.status, ResultStatus::Ok, "{:?}", result.data);
    assert_eq!(
        keys(&result.data),
        [
            "host",
            "port",
            "tls_mode",
            "connect_time_ms",
            "tls_handshake_ms",
            "cert_days_remaining",
            "attempt",
            "attempts"
        ]
    );
    assert_eq!(int(&result.data, "cert_days_remaining"), 10);
    // An IP address is verified without SNI.
    assert_eq!(observed.sni.lock().unwrap().as_slice(), [None]);
}

#[tokio::test]
async fn a_certificate_close_to_expiry_is_an_error_with_warning_severity() {
    let pki = pki();
    let (addr, _, _server) = tls_server(&pki, AfterHandshake::CloseNotify).await;
    let data = format!(
        r#"{{"port": {}, "tls_mode": "implicit", "verify": false, "check_cert_expiry": true, "min_cert_days": 999, "retries": 2}}"#,
        addr.port()
    );
    let sleeps = Sleeps::default();
    let result = executor(&sleeps)
        .execute(&check(&data, "127.0.0.1"))
        .await
        .unwrap();
    assert_eq!(result.status, ResultStatus::Error);
    assert_eq!(
        keys(&result.data),
        [
            "error_type",
            "error_msg",
            "host",
            "port",
            "tls_mode",
            "attempt",
            "attempts",
            "severity",
            "cert_days_remaining",
            "connect_time_ms",
            "tls_handshake_ms"
        ]
    );
    assert_eq!(text(&result.data, "error_type"), "cert_expiry");
    assert_eq!(
        text(&result.data, "error_msg"),
        "Certificate expires in 10 days"
    );
    assert_eq!(text(&result.data, "severity"), "warning");
    assert_eq!(int(&result.data, "attempt"), 1);
    assert_eq!(int(&result.data, "attempts"), 3);
    assert!(sleeps.calls().is_empty(), "cert_expiry is not retried");
}

#[tokio::test]
async fn an_expired_certificate_has_negative_days_without_verification() {
    let pki = pki();
    let (addr, _, _server) = tls_server(&pki, AfterHandshake::CloseNotify).await;
    let data = format!(
        r#"{{"port": {}, "tls_mode": "implicit", "verify": false, "check_cert_expiry": true, "min_cert_days": 0}}"#,
        addr.port()
    );
    let result = TcpExecutor::default()
        .with_wall_clock(Arc::new(|| NOT_AFTER as f64 + 3600.0))
        .execute(&check(&data, "127.0.0.1"))
        .await
        .unwrap();
    assert_eq!(text(&result.data, "error_type"), "cert_expiry");
    assert_eq!(int(&result.data, "cert_days_remaining"), -1);
    assert_eq!(
        text(&result.data, "error_msg"),
        "Certificate expires in -1 days"
    );
}

#[tokio::test]
async fn verification_uses_the_trust_store_and_failures_are_retried() {
    let pki = pki();
    let (addr, observed, _server) = tls_server(&pki, AfterHandshake::CloseNotify).await;
    let data = format!(
        r#"{{"port": {}, "tls_mode": "implicit", "retries": 1}}"#,
        addr.port()
    );
    let mut roots = RootCertStore::empty();
    roots.add(pki.ca.clone()).unwrap();
    let trusted = executor(&Sleeps::default())
        .with_roots(Arc::new(roots))
        .execute(&check(&data, "localhost"))
        .await
        .unwrap();
    assert_eq!(trusted.status, ResultStatus::Ok, "{:?}", trusted.data);
    assert_eq!(
        observed.sni.lock().unwrap().as_slice(),
        [Some("localhost".to_owned())]
    );

    let sleeps = Sleeps::default();
    let untrusted = executor(&sleeps)
        .with_roots(Arc::new(RootCertStore::empty()))
        .execute(&check(&data, "localhost"))
        .await
        .unwrap();
    assert_eq!(text(&untrusted.data, "error_type"), "tls_error");
    assert!(text(&untrusted.data, "error_msg").starts_with("TLS handshake failed: "));
    assert_eq!(int(&untrusted.data, "attempt"), 2);
    assert_eq!(sleeps.calls().len(), 1);
}

#[tokio::test]
async fn the_sni_override_is_sent_and_verified() {
    let pki = pki();
    let (addr, observed, _server) = tls_server(&pki, AfterHandshake::CloseNotify).await;
    let data = format!(
        r#"{{"port": {}, "tls_mode": "implicit", "sni": "override.example.com", "verify": false}}"#,
        addr.port()
    );
    let result = executor(&Sleeps::default())
        .execute(&check(&data, "127.0.0.1"))
        .await
        .unwrap();
    assert_eq!(result.status, ResultStatus::Ok, "{:?}", result.data);
    assert_eq!(
        observed.sni.lock().unwrap().as_slice(),
        [Some("override.example.com".to_owned())]
    );

    let mut roots = RootCertStore::empty();
    roots.add(pki.ca.clone()).unwrap();
    let verified = format!(
        r#"{{"port": {}, "tls_mode": "implicit", "sni": "override.example.com", "retries": 0}}"#,
        addr.port()
    );
    let result = executor(&Sleeps::default())
        .with_roots(Arc::new(roots))
        .execute(&check(&verified, "127.0.0.1"))
        .await
        .unwrap();
    assert_eq!(
        text(&result.data, "error_type"),
        "tls_error",
        "the name is checked"
    );
}

#[tokio::test]
async fn a_non_string_sni_fails_the_handshake_like_python() {
    let pki = pki();
    let (addr, _, _server) = tls_server(&pki, AfterHandshake::CloseNotify).await;
    let data = format!(
        r#"{{"port": {}, "tls_mode": "implicit", "sni": 5, "verify": false, "retries": 0}}"#,
        addr.port()
    );
    let result = executor(&Sleeps::default())
        .execute(&check(&data, "127.0.0.1"))
        .await
        .unwrap();
    assert_eq!(text(&result.data, "error_type"), "tls_error");
    assert_eq!(
        text(&result.data, "error_msg"),
        "TLS handshake failed: 'int' object has no attribute 'encode'"
    );
}

#[tokio::test]
async fn a_silent_server_is_a_tls_timeout() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let _server = tokio::spawn(async move {
        let mut held = Vec::new();
        while let Ok((socket, _)) = listener.accept().await {
            held.push(socket);
        }
    });
    let sleeps = Sleeps::default();
    let data = format!(
        r#"{{"port": {port}, "tls_mode": "implicit", "tls_handshake_timeout": 0.2, "retries": 1}}"#
    );
    let result = executor(&sleeps)
        .execute(&check(&data, "127.0.0.1"))
        .await
        .unwrap();
    assert_eq!(text(&result.data, "error_type"), "tls_timeout");
    assert_eq!(text(&result.data, "error_msg"), "TLS handshake timed out");
    assert_eq!(int(&result.data, "attempt"), 2);
}

#[tokio::test]
async fn closing_without_close_notify_is_an_unexpected_error() {
    let pki = pki();
    let (addr, _, _server) = tls_server(&pki, AfterHandshake::DropConnection).await;
    let data = format!(
        r#"{{"port": {}, "tls_mode": "implicit", "verify": false, "retries": 2}}"#,
        addr.port()
    );
    let result = executor(&Sleeps::default())
        .execute(&check(&data, "127.0.0.1"))
        .await
        .unwrap();
    assert_eq!(result.status, ResultStatus::Error);
    assert_eq!(
        keys(&result.data),
        [
            "error_type",
            "error_msg",
            "host",
            "port",
            "tls_mode",
            "attempt",
            "attempts"
        ]
    );
    assert_eq!(text(&result.data, "error_type"), "unexpected_error");
    assert_eq!(text(&result.data, "error_msg"), "");
    assert_eq!(int(&result.data, "attempt"), 1);
}

#[tokio::test]
async fn application_data_before_close_notify_is_an_unexpected_error() {
    let pki = pki();
    let (addr, _, _server) = tls_server(&pki, AfterHandshake::SendData).await;
    let data = format!(
        r#"{{"port": {}, "tls_mode": "implicit", "verify": false, "check_cert_expiry": true, "min_cert_days": 999}}"#,
        addr.port()
    );
    let result = executor(&Sleeps::default())
        .execute(&check(&data, "127.0.0.1"))
        .await
        .unwrap();
    // The failing close replaces even the cert_expiry outcome.
    assert_eq!(text(&result.data, "error_type"), "unexpected_error");
    assert!(
        text(&result.data, "error_msg").contains("APPLICATION_DATA_AFTER_CLOSE_NOTIFY"),
        "{:?}",
        result.data
    );
}

async fn run_dialogue(
    greeting: &'static [u8],
    respond: Responder,
    extra: &str,
) -> (CheckResult, Arc<Observed>) {
    let pki = pki();
    let (addr, observed, _server) = dialogue_server(&pki, greeting, respond).await;
    let data = format!(
        r#"{{"port": {}, "tls_mode": "starttls", "verify": false, "tls_handshake_timeout": 2.0, "retries": 0{extra}}}"#,
        addr.port()
    );
    let result = executor(&Sleeps::default())
        .execute(&check(&data, "127.0.0.1"))
        .await
        .unwrap();
    (result, observed)
}

#[tokio::test]
async fn starttls_dialogues_upgrade_for_every_protocol() {
    type Case = (
        &'static str,
        &'static [u8],
        Responder,
        &'static [&'static [u8]],
    );
    let cases: [Case; 3] = [
        (
            "smtp",
            SMTP_GREETING,
            smtp,
            &[b"EHLO nyxmon.invalid\r\n", b"STARTTLS\r\n"],
        ),
        ("imap", IMAP_GREETING, imap, &[b"a1 STARTTLS\r\n"]),
        ("sieve", SIEVE_GREETING, sieve, &[b"STARTTLS\r\n"]),
    ];
    for (protocol, greeting, respond, lines) in cases {
        let (result, observed) = run_dialogue(
            greeting,
            respond,
            &format!(r#", "starttls_protocol": "{protocol}", "check_cert_expiry": true, "min_cert_days": 5"#),
        )
        .await;
        assert_eq!(
            result.status,
            ResultStatus::Ok,
            "{protocol}: {:?}",
            result.data
        );
        assert_eq!(
            keys(&result.data),
            [
                "host",
                "port",
                "tls_mode",
                "starttls_protocol",
                "connect_time_ms",
                "tls_handshake_ms",
                "cert_days_remaining",
                "attempt",
                "attempts"
            ]
        );
        assert_eq!(text(&result.data, "starttls_protocol"), protocol);
        assert_eq!(
            observed.lines.lock().unwrap().as_slice(),
            lines,
            "{protocol}"
        );
        assert_eq!(observed.sni.lock().unwrap().len(), 1, "{protocol} upgraded");
    }
}

#[tokio::test]
async fn generic_starttls_can_read_the_greeting_first() {
    let (result, observed) = run_dialogue(
        b"220 custom service ready\r\n",
        generic,
        r#", "starttls_read_greeting": true"#,
    )
    .await;
    assert_eq!(result.status, ResultStatus::Ok, "{:?}", result.data);
    assert_eq!(text(&result.data, "starttls_protocol"), "generic");
    assert_eq!(observed.lines.lock().unwrap().as_slice(), [b"STARTTLS\r\n"]);
}

#[tokio::test]
async fn generic_starttls_against_a_greeting_server_fails() {
    // Without reading the greeting, the generic probe takes the IMAP
    // greeting for the STARTTLS reply and the handshake fails.
    let (result, observed) = run_dialogue(IMAP_GREETING, imap, "").await;
    assert_eq!(result.status, ResultStatus::Error);
    assert!(observed.sni.lock().unwrap().is_empty(), "never upgraded");
}

#[tokio::test]
async fn a_starttls_rejection_reports_stage_and_response() {
    let (result, _) = run_dialogue(
        SMTP_GREETING,
        smtp_refusing,
        r#", "starttls_protocol": "smtp""#,
    )
    .await;
    assert_eq!(
        keys(&result.data),
        [
            "error_type",
            "error_msg",
            "host",
            "port",
            "tls_mode",
            "attempt",
            "attempts",
            "starttls_protocol",
            "starttls_response",
            "starttls_stage"
        ]
    );
    assert_eq!(text(&result.data, "error_type"), "starttls_rejected");
    assert_eq!(
        text(&result.data, "error_msg"),
        "STARTTLS rejected at starttls: 454 4.7.0 TLS not available"
    );
    assert_eq!(text(&result.data, "starttls_stage"), "starttls");
}

#[tokio::test]
async fn a_closed_dialogue_is_retried() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let _server = tokio::spawn(async move {
        while let Ok((mut socket, _)) = listener.accept().await {
            let _ = socket
                .write_all(b"220-mail.test partial greeting\r\n")
                .await;
        }
    });
    let sleeps = Sleeps::default();
    let data = format!(
        r#"{{"port": {port}, "tls_mode": "starttls", "starttls_protocol": "smtp", "retries": 1}}"#
    );
    let result = executor(&sleeps)
        .execute(&check(&data, "127.0.0.1"))
        .await
        .unwrap();
    assert_eq!(
        text(&result.data, "error_type"),
        "starttls_connection_closed"
    );
    assert_eq!(int(&result.data, "attempt"), 2);
    assert_eq!(sleeps.calls().len(), 1);
}

#[tokio::test]
async fn a_non_string_starttls_command_is_an_unexpected_error() {
    let (result, _) = run_dialogue(b"", generic, r#", "starttls_command": 5"#).await;
    assert_eq!(
        keys(&result.data),
        [
            "error_type",
            "error_msg",
            "host",
            "port",
            "tls_mode",
            "attempt",
            "attempts"
        ]
    );
    assert_eq!(text(&result.data, "error_type"), "unexpected_error");
    assert_eq!(
        text(&result.data, "error_msg"),
        "'int' object has no attribute 'encode'"
    );
}

#[tokio::test]
async fn configuration_problems_never_connect() {
    let executor = executor(&Sleeps::default());
    let invalid = executor
        .execute(&check(r#"{"port": 0}"#, "127.0.0.1"))
        .await
        .unwrap();
    assert_eq!(keys(&invalid.data), ["error_type", "error_msg"]);
    assert_eq!(text(&invalid.data, "error_type"), "configuration_error");
    assert_eq!(
        text(&invalid.data, "error_msg"),
        "port must be an integer between 1 and 65535"
    );

    let no_host = executor
        .execute(&check(r#"{"port": 1}"#, ""))
        .await
        .unwrap();
    assert_eq!(text(&no_host.data, "error_msg"), HOST_REQUIRED);

    assert_eq!(
        executor
            .execute(&check(r#"{"port": 1, "tls_mode": []}"#, "h"))
            .await
            .unwrap_err(),
        PyException::TYPE_ERROR
    );
    assert_eq!(
        executor
            .execute(&check(r#"{"port": 1}"#, "https://[::1"))
            .await
            .unwrap_err(),
        PyException::VALUE_ERROR
    );
}

#[test]
fn certificate_days_round_down_like_python() {
    assert_eq!(cert_days_remaining(10 * 86_400, 0.0), 10);
    assert_eq!(cert_days_remaining(10 * 86_400, 0.5), 9);
    assert_eq!(cert_days_remaining(0, 0.0), 0);
    assert_eq!(cert_days_remaining(0, 1.0), -1);
    assert_eq!(cert_days_remaining(0, 86_400.0), -1);
    assert_eq!(cert_days_remaining(0, 86_400.5), -2);
    assert_eq!(py_floor_div(-0.0, 86_400.0).to_bits(), (-0.0_f64).to_bits());
}

#[tokio::test]
async fn names_rustls_cannot_send_still_connect_without_verification() {
    let pki = pki();
    let (addr, observed, _server) = tls_server(&pki, AfterHandshake::CloseNotify).await;
    for sni in ["127.0.0.1.", "a b", "-x.example"] {
        let data = format!(
            r#"{{"port": {}, "tls_mode": "implicit", "sni": "{sni}", "verify": false, "retries": 0}}"#,
            addr.port()
        );
        let result = executor(&Sleeps::default())
            .execute(&check(&data, "127.0.0.1"))
            .await
            .unwrap();
        assert_eq!(result.status, ResultStatus::Ok, "{sni}: {:?}", result.data);
    }
    // Sent without SNI.
    assert!(observed.sni.lock().unwrap().iter().all(Option::is_none));
}

/// PLAN.md D-9 (P3): with verification, a name rustls cannot represent
/// fails closed as a name mismatch, even when the certificate carries that
/// exact name as a DNS SAN (OpenSSL's `X509_check_host` could match it).
#[tokio::test]
async fn names_rustls_cannot_send_fail_verification() {
    let names = ["127.0.0.1.", "a b", "-x.example", "x:y"];
    let pki = pki_for(
        names
            .iter()
            .map(|name| SanType::DnsName((*name).try_into().unwrap()))
            .collect(),
    );
    let (addr, observed, _server) = tls_server(&pki, AfterHandshake::CloseNotify).await;
    for sni in names {
        let data = format!(
            r#"{{"port": {}, "tls_mode": "implicit", "sni": "{sni}", "retries": 1}}"#,
            addr.port()
        );
        let result = executor(&Sleeps::default())
            .with_roots(trusting(&pki))
            .execute(&check(&data, "127.0.0.1"))
            .await
            .unwrap();
        assert_eq!(result.status, ResultStatus::Error, "{sni}");
        assert_eq!(text(&result.data, "error_type"), "tls_error", "{sni}");
        assert_eq!(
            text(&result.data, "error_msg"),
            format!("TLS handshake failed: {UNVERIFIABLE_NAME}"),
            "{sni}"
        );
        assert_eq!(
            text(&result.data, "error_msg"),
            "TLS handshake failed: server name cannot be verified: not a valid DNS name or IP address"
        );
        // Retryable like every tls_error.
        assert_eq!(result.data.get("attempts"), Some(&PyJson::Int(2)), "{sni}");
    }
    // No handshake completed.
    assert!(observed.sni.lock().unwrap().is_empty());
}

#[tokio::test]
async fn names_python_refuses_fail_the_handshake() {
    let pki = pki();
    let (addr, _, _server) = tls_server(&pki, AfterHandshake::CloseNotify).await;
    for (sni, message) in [
        (
            ".example",
            "TLS handshake failed: server_hostname cannot be an empty string or start with a leading dot.",
        ),
        ("a\\u0000b", "TLS handshake failed: embedded null character"),
    ] {
        let data = format!(
            r#"{{"port": {}, "tls_mode": "implicit", "sni": "{sni}", "verify": false, "retries": 0}}"#,
            addr.port()
        );
        let result = executor(&Sleeps::default())
            .execute(&check(&data, "127.0.0.1"))
            .await
            .unwrap();
        assert_eq!(text(&result.data, "error_type"), "tls_error", "{sni}");
        assert_eq!(text(&result.data, "error_msg"), message);
    }
}

/// The loopback interface name for a scoped IPv6 literal.
fn loopback_interface() -> &'static str {
    if cfg!(target_os = "linux") {
        "lo"
    } else {
        "lo0"
    }
}

/// Serve TLS on `[::1]` for any number of connections; `None` without an
/// IPv6 loopback.
async fn ipv6_tls_server(pki: &Pki) -> Option<(u16, JoinHandle<()>)> {
    let listener = TcpListener::bind("[::1]:0").await.ok()?;
    let port = listener.local_addr().unwrap().port();
    let acceptor = TlsAcceptor::from(pki.server.clone());
    let observed = Observed::default();
    let task = tokio::spawn(async move {
        while let Ok((socket, _)) = listener.accept().await {
            serve_tls(
                socket,
                acceptor.clone(),
                AfterHandshake::CloseNotify,
                &observed,
            )
            .await;
        }
    });
    Some((port, task))
}

#[tokio::test]
async fn a_scoped_ipv6_host_follows_the_platform_resolver() {
    let host = format!("::1%{}", loopback_interface());
    let pki = pki();
    let Some((port, _server)) = ipv6_tls_server(&pki).await else {
        return; // No IPv6 loopback on this host.
    };
    let data =
        format!(r#"{{"port": {port}, "tls_mode": "implicit", "verify": false, "retries": 0}}"#);
    let result = executor(&Sleeps::default())
        .execute(&check(&data, &host))
        .await
        .unwrap();
    assert_eq!(text(&result.data, "host"), host);
    if cfg!(target_os = "linux") {
        // glibc's getaddrinfo accepts a zone only on link-local addresses, so
        // the attempt fails, as anyio's does there (checked with Python 3.13
        // and anyio on Debian).
        assert_eq!(text(&result.data, "error_type"), "connection_error");
        assert_eq!(
            text(&result.data, "error_msg"),
            format!("Connection to {host}:{port} failed: All connection attempts failed")
        );
        return;
    }
    assert_eq!(result.status, ResultStatus::Ok, "{:?}", result.data);

    // Verified, it fails closed even with the exact name as a DNS SAN
    // (PLAN.md D-9).
    let named = pki_for(vec![SanType::DnsName(host.as_str().try_into().unwrap())]);
    let Some((port, _server)) = ipv6_tls_server(&named).await else {
        return;
    };
    let verified = format!(r#"{{"port": {port}, "tls_mode": "implicit", "retries": 0}}"#);
    let result = executor(&Sleeps::default())
        .with_roots(trusting(&named))
        .execute(&check(&verified, &host))
        .await
        .unwrap();
    assert_eq!(text(&result.data, "error_type"), "tls_error");
    assert_eq!(
        text(&result.data, "error_msg"),
        format!("TLS handshake failed: {UNVERIFIABLE_NAME}")
    );
}
