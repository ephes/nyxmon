//! The TCP check executor (`executors/tcp_executor.py:TcpCheckExecutor`):
//! connect (Happy Eyeballs), optional implicit TLS or a STARTTLS dialogue,
//! certificate expiry, retries. PLAN.md sections 1.6 (result shapes) and
//! 5.12.
//!
//! Python closes the stream of every attempt with anyio's standard TLS
//! closing handshake (`TLSStream.aclose`): it sends `close_notify` and then
//! waits, without a time limit, for the peer's `close_notify`. A peer that
//! closes the connection without one, resets it or sends application data
//! first makes that close raise, which replaces the attempt's outcome with
//! `unexpected_error`. [`close_tls`] reproduces this.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use nyxmon_core::config::ConfigOutcome;
use nyxmon_core::config::PyException;
use nyxmon_core::config::tcp::{ConnectTarget, HOST_REQUIRED, TcpConfig, TlsMode, connect_target};
use nyxmon_core::model::{Check, CheckResult, ResultStatus};
use nyxmon_core::pyjson::{PyDict, PyJson, py_str, py_truthy, py_type_name};
use std::net::{IpAddr, Ipv4Addr};

use rustls::RootCertStore;
use rustls::pki_types::ServerName;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::time::Instant;
use tokio_rustls::TlsConnector;
use tokio_rustls::client::TlsStream;

use crate::happy_eyeballs::{self, ALL_ATTEMPTS_FAILED, ConnectError};
use crate::idna2008::idna2008_resolve;
use crate::pyos::os_error_str;
use crate::starttls::send_starttls_command;
use crate::tls;

/// `TcpCheckError`: a structured failure of one attempt.
#[derive(Clone, Debug, PartialEq)]
pub struct TcpCheckError {
    pub error_type: &'static str,
    pub message: String,
    pub retryable: bool,
    /// Extra result keys, appended after the standard ones.
    pub data: PyDict,
}

impl TcpCheckError {
    pub fn new(error_type: &'static str, message: String, retryable: bool) -> Self {
        Self::with_data(error_type, message, retryable, PyDict::new())
    }

    pub fn with_data(
        error_type: &'static str,
        message: String,
        retryable: bool,
        data: PyDict,
    ) -> Self {
        TcpCheckError {
            error_type,
            message,
            retryable,
            data,
        }
    }
}

/// Why one attempt failed.
#[derive(Clone, Debug, PartialEq)]
pub enum AttemptError {
    /// A `TcpCheckError`.
    Check(TcpCheckError),
    /// Any other exception: `unexpected_error` with `str(exc)`, no retry.
    Unexpected(String),
}

impl From<TcpCheckError> for AttemptError {
    fn from(err: TcpCheckError) -> Self {
        AttemptError::Check(err)
    }
}

/// Seconds since the epoch (`time.time()`), injectable for tests.
pub type WallClock = Arc<dyn Fn() -> f64 + Send + Sync>;

/// `anyio.sleep`, injectable for tests.
pub type Sleep = Arc<dyn Fn(Duration) -> Pin<Box<dyn Future<Output = ()> + Send>> + Send + Sync>;

/// The system clock.
pub fn system_wall_clock() -> WallClock {
    Arc::new(|| {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0.0, |elapsed| elapsed.as_secs_f64())
    })
}

/// `tokio::time::sleep`.
pub fn tokio_sleep() -> Sleep {
    Arc::new(|duration| Box::pin(tokio::time::sleep(duration)))
}

/// A duration from non-negative, finite seconds (`-0.0` is zero).
fn seconds(value: f64) -> Duration {
    Duration::try_from_secs_f64(value).unwrap_or(Duration::ZERO)
}

/// Milliseconds since `start`, truncated (`int((clock() - start) * 1000)`).
fn elapsed_ms(start: Instant) -> i64 {
    i64::try_from(start.elapsed().as_millis()).unwrap_or(i64::MAX)
}

/// Python's float floor division `a // b` (`float_floor_div`).
fn py_floor_div(a: f64, b: f64) -> f64 {
    let modulo = a % b;
    let mut div = (a - modulo) / b;
    if modulo != 0.0 && (b < 0.0) != (modulo < 0.0) {
        div -= 1.0;
    }
    if div == 0.0 {
        return 0.0_f64.copysign(a / b);
    }
    let floor = div.floor();
    if div - floor > 0.5 {
        floor + 1.0
    } else {
        floor
    }
}

/// `_cert_days_remaining`: whole days until `notAfter`, rounded down.
pub fn cert_days_remaining(not_after: i64, now: f64) -> i64 {
    let days = py_floor_div(not_after as f64 - now, 86400.0);
    // In range for any certificate date; `as` saturates otherwise.
    days as i64
}

/// The executor. Cheap to clone; holds no connections between checks.
#[derive(Clone)]
pub struct TcpExecutor {
    wall_clock: WallClock,
    sleep: Sleep,
    /// Trust anchors for `verify=true`; the system store when `None`.
    roots: Option<Arc<RootCertStore>>,
}

impl Default for TcpExecutor {
    fn default() -> Self {
        TcpExecutor {
            wall_clock: system_wall_clock(),
            sleep: tokio_sleep(),
            roots: None,
        }
    }
}

impl TcpExecutor {
    /// Replace the clock behind `cert_days_remaining`.
    pub fn with_wall_clock(mut self, clock: WallClock) -> Self {
        self.wall_clock = clock;
        self
    }

    /// Replace the sleep between attempts.
    pub fn with_sleep(mut self, sleep: Sleep) -> Self {
        self.sleep = sleep;
        self
    }

    /// Trust `roots` instead of the system store when verifying.
    pub fn with_roots(mut self, roots: Arc<RootCertStore>) -> Self {
        self.roots = Some(roots);
        self
    }

    /// `TcpCheckExecutor.execute`.
    ///
    /// # Errors
    /// The Python exception that would escape `execute` (the runner records
    /// it as `executor_exception`).
    pub async fn execute(&self, check: &Check) -> Result<CheckResult, PyException> {
        let config = match TcpConfig::from_data(&check.data) {
            ConfigOutcome::Ok(config) => config,
            ConfigOutcome::ConfigError(message) => {
                return Ok(error_result(
                    check,
                    "configuration_error",
                    message,
                    PyDict::new(),
                ));
            }
            ConfigOutcome::PyException(exc) => return Err(exc),
        };
        let Some(host) = config.target_host(&check.url)? else {
            return Ok(error_result(
                check,
                "configuration_error",
                HOST_REQUIRED.to_owned(),
                PyDict::new(),
            ));
        };

        let attempts = config.attempts();
        for attempt in 1..=attempts {
            match self.attempt_once(&host, &config).await {
                Ok(mut data) => {
                    data.insert("attempt".to_owned(), PyJson::Int(attempt));
                    data.insert("attempts".to_owned(), PyJson::Int(attempts));
                    return Ok(CheckResult::new(check.check_id, ResultStatus::Ok, data));
                }
                Err(AttemptError::Check(exc)) => {
                    let mut data = PyDict::new();
                    data.insert(
                        "error_type".to_owned(),
                        PyJson::Str(exc.error_type.to_owned()),
                    );
                    data.insert("error_msg".to_owned(), PyJson::Str(exc.message));
                    base_keys(&mut data, &host, &config, attempt, attempts);
                    if config.tls_mode == TlsMode::Starttls {
                        data.insert(
                            "starttls_protocol".to_owned(),
                            PyJson::Str(config.starttls_protocol.as_str().to_owned()),
                        );
                    }
                    data.extend(exc.data);
                    if exc.retryable && attempt < attempts {
                        (self.sleep)(seconds(config.retry_delay)).await;
                        continue;
                    }
                    return Ok(CheckResult::new(check.check_id, ResultStatus::Error, data));
                }
                Err(AttemptError::Unexpected(message)) => {
                    let mut extra = PyDict::new();
                    base_keys(&mut extra, &host, &config, attempt, attempts);
                    return Ok(error_result(check, "unexpected_error", message, extra));
                }
            }
        }
        // Unreachable: there is at least one attempt and each one returns.
        let mut extra = PyDict::new();
        extra.insert("host".to_owned(), host);
        extra.insert("port".to_owned(), PyJson::Int(config.port));
        extra.insert(
            "tls_mode".to_owned(),
            PyJson::Str(config.tls_mode.as_str().to_owned()),
        );
        Ok(error_result(
            check,
            "unknown_error",
            "unknown failure while executing TCP check".to_owned(),
            extra,
        ))
    }

    /// `_attempt_once`: one connection including TLS negotiation.
    async fn attempt_once(
        &self,
        host: &PyJson,
        config: &TcpConfig,
    ) -> Result<PyDict, AttemptError> {
        let (mut stream, connect_time_ms) = connect(host, config).await?;
        if config.tls_mode == TlsMode::None {
            drop(stream);
            return Ok(success_data(host, config, connect_time_ms, None, None));
        }
        if config.tls_mode == TlsMode::Starttls {
            send_starttls_command(&mut stream, config, seconds(config.tls_handshake_timeout))
                .await?;
        }
        let (tls_stream, tls_handshake_ms) = self.negotiate_tls(stream, host, config).await?;
        let outcome =
            self.check_certificate(&tls_stream, config, connect_time_ms, tls_handshake_ms);
        // `finally: await current_stream.aclose()`: a failing close replaces
        // the outcome.
        close_tls(tls_stream)
            .await
            .map_err(AttemptError::Unexpected)?;
        let cert_days_remaining = outcome?;
        Ok(success_data(
            host,
            config,
            connect_time_ms,
            Some(tls_handshake_ms),
            cert_days_remaining,
        ))
    }

    /// `_negotiate_tls` after the optional STARTTLS dialogue: the handshake
    /// against `sni or host`, bounded by `tls_handshake_timeout`.
    async fn negotiate_tls(
        &self,
        stream: TcpStream,
        host: &PyJson,
        config: &TcpConfig,
    ) -> Result<(TlsStream<TcpStream>, i64), AttemptError> {
        let client = if config.verify {
            tls::verifying_client_config(self.roots.clone().unwrap_or_else(tls::system_roots))
        } else {
            tls::client_config(false)
        };
        let hostname = if py_truthy(&config.sni) {
            &config.sni
        } else {
            host
        };
        let start = Instant::now();
        let handshake = async {
            let name = match hostname {
                PyJson::Str(name) => idna2008_resolve(name).map_err(|err| err.to_string())?,
                other => {
                    return Err(format!(
                        "'{}' object has no attribute 'encode'",
                        py_type_name(other)
                    ));
                }
            };
            let server_name = tls_target(name, config.verify)?;
            TlsConnector::from(client)
                .connect(server_name, stream)
                .await
                .map_err(|err| err.to_string())
        };
        match tokio::time::timeout(seconds(config.tls_handshake_timeout), handshake).await {
            Err(_) => {
                Err(
                    TcpCheckError::new("tls_timeout", "TLS handshake timed out".to_owned(), true)
                        .into(),
                )
            }
            Ok(Err(message)) => Err(TcpCheckError::new(
                "tls_error",
                format!("TLS handshake failed: {message}"),
                true,
            )
            .into()),
            Ok(Ok(tls_stream)) => Ok((tls_stream, elapsed_ms(start))),
        }
    }

    /// The `check_cert_expiry` part of `_attempt_once`: the days remaining
    /// on the peer's leaf certificate, or `cert_missing`/`cert_expiry`.
    fn check_certificate(
        &self,
        tls_stream: &TlsStream<TcpStream>,
        config: &TcpConfig,
        connect_time_ms: i64,
        tls_handshake_ms: i64,
    ) -> Result<Option<i64>, AttemptError> {
        if !config.check_cert_expiry {
            return Ok(None);
        }
        let leaf = tls_stream
            .get_ref()
            .1
            .peer_certificates()
            .and_then(|certs| certs.first());
        let Some(leaf) = leaf else {
            let mut data = PyDict::new();
            data.insert("connect_time_ms".to_owned(), PyJson::Int(connect_time_ms));
            data.insert("tls_handshake_ms".to_owned(), PyJson::Int(tls_handshake_ms));
            return Err(TcpCheckError::with_data(
                "cert_missing",
                "TLS certificate not available from peer".to_owned(),
                false,
                data,
            )
            .into());
        };
        let (_, certificate) = x509_parser::parse_x509_certificate(leaf.as_ref())
            .map_err(|err| AttemptError::Unexpected(err.to_string()))?;
        let not_after = certificate.validity().not_after.timestamp();
        let days = cert_days_remaining(not_after, (self.wall_clock)());
        if days < config.min_cert_days {
            let mut data = PyDict::new();
            data.insert("severity".to_owned(), PyJson::Str("warning".to_owned()));
            data.insert("cert_days_remaining".to_owned(), PyJson::Int(days));
            data.insert("connect_time_ms".to_owned(), PyJson::Int(connect_time_ms));
            data.insert("tls_handshake_ms".to_owned(), PyJson::Int(tls_handshake_ms));
            return Err(TcpCheckError::with_data(
                "cert_expiry",
                format!("Certificate expires in {days} days"),
                false,
                data,
            )
            .into());
        }
        Ok(Some(days))
    }
}

/// The server name for the `server_hostname` Python's `ssl` gets.
///
/// CPython rejects an empty name, a leading dot and an embedded NUL;
/// OpenSSL sends any other non-IP name of at most 255 bytes as SNI and,
/// when verifying, checks it against the certificate (`X509_check_host`).
/// rustls only accepts valid DNS names and IP addresses. Any other name (a
/// scoped IPv6 literal, `127.0.0.1.`, a name with `:` or a space) is a P3
/// divergence (PLAN.md D-9): without verification the handshake is made
/// without SNI; with verification the attempt fails closed as a name
/// mismatch, before the handshake.
fn tls_target(name: String, verify: bool) -> Result<ServerName<'static>, String> {
    if name.is_empty() || name.starts_with('.') {
        return Err(
            "server_hostname cannot be an empty string or start with a leading dot.".to_owned(),
        );
    }
    if name.contains('\0') {
        return Err("embedded null character".to_owned());
    }
    if name.len() > 255 {
        return Err("SNI name longer than 255 bytes".to_owned());
    }
    match ServerName::try_from(name) {
        Ok(server_name) => Ok(server_name),
        // An IP address name: rustls sends no SNI for it, and nothing
        // verifies it.
        Err(_) if !verify => Ok(ServerName::IpAddress(
            IpAddr::V4(Ipv4Addr::UNSPECIFIED).into(),
        )),
        Err(_) => Err(UNVERIFIABLE_NAME.to_owned()),
    }
}

/// The `tls_error` detail for a name rustls cannot represent when
/// verifying (PLAN.md D-9).
pub const UNVERIFIABLE_NAME: &str =
    "server name cannot be verified: not a valid DNS name or IP address";

/// `host, port, tls_mode, attempt, attempts` of the error results.
fn base_keys(data: &mut PyDict, host: &PyJson, config: &TcpConfig, attempt: i64, attempts: i64) {
    data.insert("host".to_owned(), host.clone());
    data.insert("port".to_owned(), PyJson::Int(config.port));
    data.insert(
        "tls_mode".to_owned(),
        PyJson::Str(config.tls_mode.as_str().to_owned()),
    );
    data.insert("attempt".to_owned(), PyJson::Int(attempt));
    data.insert("attempts".to_owned(), PyJson::Int(attempts));
}

/// The data of a successful attempt (without `attempt`/`attempts`).
fn success_data(
    host: &PyJson,
    config: &TcpConfig,
    connect_time_ms: i64,
    tls_handshake_ms: Option<i64>,
    cert_days_remaining: Option<i64>,
) -> PyDict {
    let mut data = PyDict::new();
    data.insert("host".to_owned(), host.clone());
    data.insert("port".to_owned(), PyJson::Int(config.port));
    data.insert(
        "tls_mode".to_owned(),
        PyJson::Str(config.tls_mode.as_str().to_owned()),
    );
    if config.tls_mode == TlsMode::Starttls {
        data.insert(
            "starttls_protocol".to_owned(),
            PyJson::Str(config.starttls_protocol.as_str().to_owned()),
        );
    }
    data.insert("connect_time_ms".to_owned(), PyJson::Int(connect_time_ms));
    if let Some(ms) = tls_handshake_ms {
        data.insert("tls_handshake_ms".to_owned(), PyJson::Int(ms));
    }
    if let Some(days) = cert_days_remaining {
        data.insert("cert_days_remaining".to_owned(), PyJson::Int(days));
    }
    data
}

/// `TcpCheckExecutor._error`.
fn error_result(check: &Check, error_type: &str, message: String, extra: PyDict) -> CheckResult {
    let mut data = PyDict::new();
    data.insert("error_type".to_owned(), PyJson::Str(error_type.to_owned()));
    data.insert("error_msg".to_owned(), PyJson::Str(message));
    data.extend(extra);
    CheckResult::new(check.check_id, ResultStatus::Error, data)
}

/// Why resolving or connecting failed before the connect timeout.
enum ConnectFailure {
    Connect(ConnectError),
    /// The host cannot be IDNA-encoded (`IDNAError` escapes `_connect`).
    Unexpected(String),
}

/// Resolve and connect to `host` (`anyio.connect_tcp`).
async fn open(host: &PyJson, port: u16) -> Result<TcpStream, ConnectFailure> {
    let target = match connect_target(host) {
        ConnectTarget::Name(name) => ConnectTarget::Name(
            idna2008_resolve(&name).map_err(|err| ConnectFailure::Unexpected(err.to_string()))?,
        ),
        other => other,
    };
    happy_eyeballs::connect_tcp(&target, port)
        .await
        .map_err(ConnectFailure::Connect)
}

/// `_connect`: bound `connection` by `connect_timeout` and classify its
/// failure for `host:port`.
async fn bounded_connect<F>(
    host: &PyJson,
    config: &TcpConfig,
    connection: F,
) -> Result<(TcpStream, i64), AttemptError>
where
    F: Future<Output = Result<TcpStream, ConnectFailure>>,
{
    let start = Instant::now();
    let target = format!("{}:{}", py_str(host), config.port);
    match tokio::time::timeout(seconds(config.connect_timeout), connection).await {
        Err(_) => Err(TcpCheckError::new(
            "connect_timeout",
            format!("Connection to {target} timed out"),
            true,
        )
        .into()),
        Ok(Err(ConnectFailure::Unexpected(message))) => Err(AttemptError::Unexpected(message)),
        Ok(Err(ConnectFailure::Connect(err))) => {
            let reason = match err {
                ConnectError::Resolve(err) => os_error_str(&err),
                ConnectError::AllFailed => ALL_ATTEMPTS_FAILED.to_owned(),
            };
            Err(TcpCheckError::new(
                "connection_error",
                format!("Connection to {target} failed: {reason}"),
                true,
            )
            .into())
        }
        Ok(Ok(stream)) => Ok((stream, elapsed_ms(start))),
    }
}

async fn connect(host: &PyJson, config: &TcpConfig) -> Result<(TcpStream, i64), AttemptError> {
    // `validate` bounds the port to 1..=65535.
    let port = u16::try_from(config.port).unwrap_or(0);
    bounded_connect(host, config, open(host, port)).await
}

/// anyio's `TLSStream.aclose` with `standard_compatible=True`: send
/// `close_notify`, then read until the peer's `close_notify`.
///
/// # Errors
/// `str(exc)` of what Python raises: an empty text (`BrokenResourceError`)
/// when the peer closes or resets the connection without `close_notify`, the
/// OpenSSL error for application data, the TLS error otherwise.
pub async fn close_tls<S: AsyncRead + AsyncWrite + Unpin>(
    mut stream: TlsStream<S>,
) -> Result<(), String> {
    stream.get_mut().1.send_close_notify();
    if stream.flush().await.is_err() {
        return Err(String::new());
    }
    let mut buffer = [0u8; 4096];
    match stream.read(&mut buffer).await {
        Ok(0) => Ok(()),
        Ok(_) => Err(
            "[SSL: APPLICATION_DATA_AFTER_CLOSE_NOTIFY] application data after close notify"
                .to_owned(),
        ),
        Err(err) if err.kind() == std::io::ErrorKind::InvalidData => Err(err.to_string()),
        Err(_) => Err(String::new()),
    }
}

#[cfg(test)]
mod tests;
