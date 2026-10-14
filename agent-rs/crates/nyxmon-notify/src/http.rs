//! The HTTP client of the notifiers, behaving like the `httpx.AsyncClient()`
//! that `notification.py` opens per request.
//!
//! - one fresh HTTP/1.1 connection per request (httpx opens a new client
//!   each time), no redirects followed, `User-Agent: python-httpx/0.28.1`,
//!   `Accept: */*`, `Connection: keep-alive`;
//! - `timeout=t` with httpx's per-phase semantics: the TCP connect plus TLS
//!   handshake within `t` (`ConnectTimeout`), then every single socket write
//!   (`WriteTimeout`) and every single socket read (`ReadTimeout`) within
//!   `t`, so a response that keeps arriving never times out;
//! - connecting like `anyio.connect_tcp` (httpx's backend): the resolved
//!   addresses in anyio's order, a new attempt every 250 ms or as soon as
//!   the newest one fails, the first connection wins;
//! - TLS: rustls with the `ring` provider and the Mozilla roots of
//!   `webpki-roots` (httpx uses `certifi`, the same bundle), TLS 1.2+, ALPN
//!   `http/1.1`.
//!
//! No `Accept-Encoding` is sent, so responses arrive uncompressed (httpx
//! announces `gzip, deflate` and decodes); the notifiers only read small JSON
//! or text bodies, so the outcome is the same. Proxy variables are not
//! honoured (PLAN.md D-13).
//!
//! Transport errors carry the httpx exception class and a Python-style
//! message (`[Errno N] …`, `timed out`); both are log text only (P2).

use std::future::Future;
use std::io;
use std::net::SocketAddr;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::task::{Context, Poll};
use std::time::Duration;

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper_util::rt::TokioIo;
use rustls::ClientConfig;
use rustls::pki_types::ServerName;
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::net::TcpStream;
use tokio::time::Sleep;
use tokio_rustls::TlsConnector;

/// `User-Agent` of httpx 0.28.1 (owner decision: kept).
pub(crate) const USER_AGENT: &str = "python-httpx/0.28.1";

/// The longest timeout used; a configured `OPSGATE_SUBMIT_TIMEOUT_SECONDS`
/// beyond it is clamped (Python passes any positive integer through).
const MAX_TIMEOUT: Duration = Duration::from_secs(10 * 365 * 86_400);

/// A timeout in seconds as the client takes it.
pub(crate) fn timeout_from_secs(seconds: f64) -> Duration {
    Duration::try_from_secs_f64(seconds)
        .unwrap_or(MAX_TIMEOUT)
        .min(MAX_TIMEOUT)
}

/// A failed exchange: the httpx exception class and its message.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct TransportError {
    pub class: &'static str,
    pub detail: String,
}

impl TransportError {
    fn new(class: &'static str, detail: impl Into<String>) -> Self {
        TransportError {
            class,
            detail: detail.into(),
        }
    }
}

/// A complete response.
#[derive(Debug)]
pub(crate) struct Response {
    pub status: u16,
    pub body: Vec<u8>,
}

/// One client per notifier endpoint, with httpx's `timeout=t`.
#[derive(Clone)]
pub(crate) struct HttpClient {
    timeout: Duration,
    tls: TlsConnector,
}

fn tls_config() -> ClientConfig {
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let roots = rustls::RootCertStore {
        roots: webpki_roots::TLS_SERVER_ROOTS.to_vec(),
    };
    let mut config = ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .expect("the ring provider supports the default protocol versions")
        .with_root_certificates(roots)
        .with_no_client_auth();
    config.alpn_protocols = vec![b"http/1.1".to_vec()];
    config
}

/// `[Errno N] <strerror>` for an OS error, like Python's `OSError.__str__`.
fn os_error_text(err: &io::Error) -> String {
    match err.raw_os_error() {
        Some(code) => {
            let text = err.to_string();
            let suffix = format!(" (os error {code})");
            let message = text.strip_suffix(&suffix).unwrap_or(&text);
            format!("[Errno {code}] {message}")
        }
        None => err.to_string(),
    }
}

/// Marks an I/O error produced by [`TimeoutIo`].
#[derive(Debug)]
struct PhaseTimeout(&'static str);

impl std::fmt::Display for PhaseTimeout {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("timed out")
    }
}

impl std::error::Error for PhaseTimeout {}

/// Bounds every single write by `timeout`, and every single read once the
/// request has been sent (httpcore's socket timeouts: the request is written
/// first, then the response is read). A timer starts when an operation has
/// to wait and is reset whenever it completes.
///
/// hyper already polls for a response while it is still writing the
/// request, so a read that waits before the request is out arms no timer.
/// The request is out at the first completed flush after hyper has taken
/// the end of the body ([`TrackedBody`]; hyper drains its write buffer
/// before it flushes the transport); that flush arms the read timer with
/// the connection task's waker (hyper drives the connection from one task).
struct TimeoutIo<S> {
    inner: S,
    timeout: Duration,
    read_timer: Option<Pin<Box<Sleep>>>,
    write_timer: Option<Pin<Box<Sleep>>>,
    /// Set by [`TrackedBody`] once hyper has taken the whole body.
    body_taken: Arc<AtomicBool>,
    /// The request has been written and flushed: reads are timed.
    sent: bool,
}

impl<S> TimeoutIo<S> {
    fn new(inner: S, timeout: Duration, body_taken: Arc<AtomicBool>) -> Self {
        TimeoutIo {
            inner,
            timeout,
            read_timer: None,
            write_timer: None,
            body_taken,
            sent: false,
        }
    }
}

/// A request body that reports when hyper has taken all of it.
struct TrackedBody {
    inner: Full<Bytes>,
    taken: Arc<AtomicBool>,
}

impl TrackedBody {
    fn new(data: Bytes, taken: Arc<AtomicBool>) -> Self {
        let inner = Full::new(data);
        if hyper::body::Body::is_end_stream(&inner) {
            taken.store(true, Ordering::SeqCst);
        }
        TrackedBody { inner, taken }
    }
}

impl hyper::body::Body for TrackedBody {
    type Data = Bytes;
    type Error = std::convert::Infallible;

    fn poll_frame(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<hyper::body::Frame<Bytes>, Self::Error>>> {
        let this = self.get_mut();
        let frame = Pin::new(&mut this.inner).poll_frame(cx);
        if matches!(frame, Poll::Ready(None)) || this.inner.is_end_stream() {
            this.taken.store(true, Ordering::SeqCst);
        }
        frame
    }

    fn is_end_stream(&self) -> bool {
        self.inner.is_end_stream()
    }

    fn size_hint(&self) -> hyper::body::SizeHint {
        self.inner.size_hint()
    }
}

fn timed_out<T>(class: &'static str) -> Poll<io::Result<T>> {
    Poll::Ready(Err(io::Error::new(
        io::ErrorKind::TimedOut,
        PhaseTimeout(class),
    )))
}

/// Poll `timer` (creating it on first use); `true` when it has fired.
fn timer_fired(
    timer: &mut Option<Pin<Box<Sleep>>>,
    timeout: Duration,
    cx: &mut Context<'_>,
) -> bool {
    let sleep = timer.get_or_insert_with(|| Box::pin(tokio::time::sleep(timeout)));
    if sleep.as_mut().poll(cx).is_ready() {
        *timer = None;
        return true;
    }
    false
}

fn poll_write_timed<T>(
    result: Poll<io::Result<T>>,
    timer: &mut Option<Pin<Box<Sleep>>>,
    timeout: Duration,
    cx: &mut Context<'_>,
) -> Poll<io::Result<T>> {
    match result {
        Poll::Ready(result) => {
            *timer = None;
            Poll::Ready(result)
        }
        Poll::Pending if timer_fired(timer, timeout, cx) => timed_out("WriteTimeout"),
        Poll::Pending => Poll::Pending,
    }
}

impl<S: AsyncRead + Unpin> AsyncRead for TimeoutIo<S> {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        match Pin::new(&mut this.inner).poll_read(cx, buf) {
            Poll::Ready(result) => {
                this.read_timer = None;
                Poll::Ready(result)
            }
            Poll::Pending if this.sent && timer_fired(&mut this.read_timer, this.timeout, cx) => {
                timed_out("ReadTimeout")
            }
            Poll::Pending => Poll::Pending,
        }
    }
}

impl<S: AsyncWrite + Unpin> AsyncWrite for TimeoutIo<S> {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        let result = Pin::new(&mut this.inner).poll_write(cx, buf);
        poll_write_timed(result, &mut this.write_timer, this.timeout, cx)
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        let result = Pin::new(&mut this.inner).poll_flush(cx);
        if matches!(result, Poll::Ready(Ok(())))
            && !this.sent
            && this.body_taken.load(Ordering::SeqCst)
        {
            // The request is out: the response read timer starts now.
            this.sent = true;
            let mut sleep = Box::pin(tokio::time::sleep(this.timeout));
            let _ = sleep.as_mut().poll(cx);
            this.read_timer = Some(sleep);
        }
        poll_write_timed(result, &mut this.write_timer, this.timeout, cx)
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().inner).poll_shutdown(cx)
    }
}

/// Happy-eyeballs delay of `anyio.connect_tcp`.
const HAPPY_EYEBALLS_DELAY: Duration = Duration::from_millis(250);

/// `anyio.connect_tcp`'s target order: the first IPv6 address first, the
/// first IPv4 address after an IPv6 one second, everything else in resolver
/// order.
fn anyio_order(resolved: Vec<SocketAddr>) -> Vec<SocketAddr> {
    let (mut v6_found, mut v4_found) = (false, false);
    let mut targets = Vec::with_capacity(resolved.len());
    for addr in resolved {
        if addr.is_ipv6() && !v6_found {
            v6_found = true;
            targets.insert(0, addr);
        } else if addr.is_ipv4() && !v4_found && v6_found {
            v4_found = true;
            targets.insert(1, addr);
        } else {
            targets.push(addr);
        }
    }
    targets
}

/// `anyio.connect_tcp`: start an attempt per address, the next one when the
/// previous attempt failed or after [`HAPPY_EYEBALLS_DELAY`]; the first
/// connection wins.
async fn connect_any(targets: Vec<SocketAddr>) -> Result<TcpStream, TransportError> {
    let mut pending = targets.into_iter();
    let mut attempts = tokio::task::JoinSet::new();
    // The newest attempt and when the next one starts regardless.
    let mut latest: Option<tokio::task::Id> = None;
    let mut next_start = tokio::time::Instant::now();
    let mut start_next = |attempts: &mut tokio::task::JoinSet<io::Result<TcpStream>>,
                          latest: &mut Option<tokio::task::Id>,
                          next_start: &mut tokio::time::Instant|
     -> bool {
        match pending.next() {
            Some(addr) => {
                *latest = Some(attempts.spawn(TcpStream::connect(addr)).id());
                *next_start = tokio::time::Instant::now() + HAPPY_EYEBALLS_DELAY;
                true
            }
            None => false,
        }
    };
    start_next(&mut attempts, &mut latest, &mut next_start);
    while !attempts.is_empty() {
        tokio::select! {
            finished = attempts.join_next_with_id() => match finished {
                Some(Ok((_, Ok(stream)))) => {
                    attempts.abort_all();
                    return Ok(stream);
                }
                Some(failed) => {
                    // A failure of the newest attempt starts the next one at
                    // once (anyio waits on the newest attempt's event).
                    let id = match &failed {
                        Ok((id, _)) => *id,
                        Err(err) => err.id(),
                    };
                    if latest == Some(id) {
                        start_next(&mut attempts, &mut latest, &mut next_start);
                    }
                }
                None => {}
            },
            () = tokio::time::sleep_until(next_start) => {
                if !start_next(&mut attempts, &mut latest, &mut next_start) {
                    // Nothing left to start: wait for the running attempts.
                    next_start = tokio::time::Instant::now() + MAX_TIMEOUT;
                }
            }
        }
    }
    // httpcore turns anyio's OSError into ConnectError with this text.
    Err(TransportError::new(
        "ConnectError",
        "All connection attempts failed",
    ))
}

/// The httpx class and message of a hyper error, looking through its
/// sources for our timeouts and OS errors.
fn classify(err: &hyper::Error, reading: bool) -> TransportError {
    let mut source: Option<&(dyn std::error::Error + 'static)> = std::error::Error::source(err);
    while let Some(cause) = source {
        if let Some(io_err) = cause.downcast_ref::<io::Error>() {
            if let Some(timeout) = io_err
                .get_ref()
                .and_then(|inner| inner.downcast_ref::<PhaseTimeout>())
            {
                return TransportError::new(timeout.0, "timed out");
            }
            let class = if reading { "ReadError" } else { "WriteError" };
            return TransportError::new(class, os_error_text(io_err));
        }
        source = cause.source();
    }
    TransportError::new("RemoteProtocolError", err.to_string())
}

/// Aborts the spawned connection driver when the exchange ends.
struct AbortOnDrop(tokio::task::JoinHandle<()>);

impl Drop for AbortOnDrop {
    fn drop(&mut self) {
        self.0.abort();
    }
}

trait Stream: AsyncRead + AsyncWrite + Unpin + Send {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> Stream for T {}

impl HttpClient {
    pub(crate) fn new(timeout: Duration) -> Self {
        HttpClient {
            timeout,
            tls: TlsConnector::from(Arc::new(tls_config())),
        }
    }

    /// TCP connect plus TLS handshake, bounded by the timeout together.
    async fn connect(&self, url: &url::Url) -> Result<Box<dyn Stream>, TransportError> {
        let https = url.scheme() == "https";
        let port = url.port_or_known_default().unwrap_or(80);
        let host = url.host().ok_or_else(|| {
            TransportError::new("UnsupportedProtocol", "Request URL is missing a host")
        })?;
        let attempt = async {
            let (targets, server_name) = match host {
                url::Host::Domain(domain) => {
                    let resolved: Vec<SocketAddr> = tokio::net::lookup_host((domain, port))
                        .await
                        .map_err(|e| TransportError::new("ConnectError", os_error_text(&e)))?
                        .collect();
                    (
                        anyio_order(resolved),
                        ServerName::try_from(domain.to_owned()).ok(),
                    )
                }
                url::Host::Ipv4(ip) => (
                    vec![SocketAddr::from((ip, port))],
                    Some(ServerName::IpAddress(std::net::IpAddr::V4(ip).into())),
                ),
                url::Host::Ipv6(ip) => (
                    vec![SocketAddr::from((ip, port))],
                    Some(ServerName::IpAddress(std::net::IpAddr::V6(ip).into())),
                ),
            };
            let tcp = connect_any(targets).await?;
            let _ = tcp.set_nodelay(true);
            if !https {
                return Ok(Box::new(tcp) as Box<dyn Stream>);
            }
            let name = server_name
                .ok_or_else(|| TransportError::new("ConnectError", "invalid server name"))?;
            let tls = self
                .tls
                .connect(name, tcp)
                .await
                .map_err(|e| TransportError::new("ConnectError", os_error_text(&e)))?;
            Ok(Box::new(tls) as Box<dyn Stream>)
        };
        match tokio::time::timeout(self.timeout, attempt).await {
            Ok(result) => result,
            Err(_) => Err(TransportError::new("ConnectTimeout", "timed out")),
        }
    }

    /// POST `body` to `url` and read the whole response.
    pub(crate) async fn post(
        &self,
        url: &str,
        headers: &[(&str, &str)],
        body: String,
    ) -> Result<Response, TransportError> {
        let url = url::Url::parse(url)
            .map_err(|e| TransportError::new("UnsupportedProtocol", e.to_string()))?;
        if url.scheme() != "http" && url.scheme() != "https" {
            return Err(TransportError::new(
                "UnsupportedProtocol",
                format!(
                    "Request URL has an unsupported protocol '{}://'.",
                    url.scheme()
                ),
            ));
        }
        let stream = self.connect(&url).await?;
        let mut target = url.path().to_owned();
        if let Some(query) = url.query() {
            target.push('?');
            target.push_str(query);
        }
        let host = match url.port() {
            Some(port) => format!("{}:{port}", url.host_str().unwrap_or_default()),
            None => url.host_str().unwrap_or_default().to_owned(),
        };
        let request = hyper::Request::post(target)
            .header("host", host)
            .header("accept", "*/*")
            .header("connection", "keep-alive")
            .header("user-agent", USER_AGENT)
            .header("content-length", body.len().to_string());
        let request = headers.iter().fold(request, |request, (name, value)| {
            request.header(*name, *value)
        });
        exchange(stream, self.timeout, request, Bytes::from(body)).await
    }
}

/// Send one request over `stream` and read the whole response, with
/// [`TimeoutIo`] bounding every read and write.
async fn exchange<S: Stream + 'static>(
    stream: S,
    timeout: Duration,
    request: hyper::http::request::Builder,
    body: Bytes,
) -> Result<Response, TransportError> {
    let body_taken = Arc::new(AtomicBool::new(false));
    let request = request
        .body(TrackedBody::new(body, body_taken.clone()))
        .map_err(|e| TransportError::new("LocalProtocolError", e.to_string()))?;
    let io = TokioIo::new(TimeoutIo::new(stream, timeout, body_taken));
    let (mut sender, connection) = hyper::client::conn::http1::handshake(io)
        .await
        .map_err(|e| classify(&e, false))?;
    let _driver = AbortOnDrop(tokio::spawn(async move {
        let _ = connection.await;
    }));
    let response = sender
        .send_request(request)
        .await
        .map_err(|e| classify(&e, true))?;
    let status = response.status().as_u16();
    let body = response
        .into_body()
        .collect()
        .await
        .map_err(|e| classify(&e, true))?
        .to_bytes()
        .to_vec();
    Ok(Response { status, body })
}

/// `urllib.parse.urlencode(fields)` as httpx encodes `data=`: `quote_plus`
/// with only ASCII letters, digits and `_.-~` left as they are.
pub(crate) fn form_urlencode(fields: &[(&str, &str)]) -> String {
    let mut out = String::new();
    for (index, (key, value)) in fields.iter().enumerate() {
        if index > 0 {
            out.push('&');
        }
        quote_plus(key, &mut out);
        out.push('=');
        quote_plus(value, &mut out);
    }
    out
}

fn quote_plus(text: &str, out: &mut String) {
    for byte in text.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => {
                out.push(char::from(byte));
            }
            b' ' => out.push('+'),
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn form_encoding_matches_urlencode() {
        // `urlencode({"a": "b c~*é", "t": "true"})` (httpx renders True as
        // "true").
        assert_eq!(
            form_urlencode(&[("a", "b c~*\u{e9}"), ("t", "true")]),
            "a=b+c~%2A%C3%A9&t=true"
        );
        assert_eq!(form_urlencode(&[("k y", "")]), "k+y=");
    }

    #[test]
    fn timeouts_are_clamped() {
        assert_eq!(timeout_from_secs(7.0), Duration::from_secs(7));
        assert_eq!(timeout_from_secs(1e300), MAX_TIMEOUT);
    }

    /// A request written into a 16-byte pipe the server drains slowly, then
    /// a response after `response_delay`; per-operation timeout 100 ms.
    async fn slow_upload(response_delay: Duration) -> Result<Response, TransportError> {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let (client, mut server) = tokio::io::duplex(16);
        tokio::spawn(async move {
            let mut received = Vec::new();
            let mut chunk = [0u8; 16];
            // Head plus a 64-byte body: about ten reads 60 ms apart.
            while !received.ends_with(&[b'x'; 64]) {
                tokio::time::sleep(Duration::from_millis(60)).await;
                match server.read(&mut chunk).await {
                    Ok(0) | Err(_) => return,
                    Ok(n) => received.extend_from_slice(&chunk[..n]),
                }
            }
            tokio::time::sleep(response_delay).await;
            let _ = server
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                .await;
        });
        let request = hyper::Request::post("/")
            .header("host", "test")
            .header("content-length", "64");
        exchange(
            client,
            Duration::from_millis(100),
            request,
            Bytes::from(vec![b'x'; 64]),
        )
        .await
    }

    /// httpx times the upload per write and the response from the moment
    /// the request is sent; an upload slower than the timeout in total must
    /// not count against the response.
    #[tokio::test]
    async fn read_timer_starts_after_the_request_is_sent() {
        // The last piece of the request still waits up to 60 ms in the pipe
        // after the final write, so 20 ms more keeps the response wait below
        // 100 ms, while the upload alone takes about 600 ms.
        let response = slow_upload(Duration::from_millis(20))
            .await
            .expect("no timeout");
        assert_eq!((response.status, response.body), (200, b"ok".to_vec()));
    }

    #[tokio::test]
    async fn response_slower_than_the_timeout_is_a_read_timeout() {
        let err = slow_upload(Duration::from_millis(250))
            .await
            .expect_err("timeout");
        assert_eq!(err, TransportError::new("ReadTimeout", "timed out"));
    }

    #[test]
    fn anyio_target_order() {
        let v4 = |n: u8| SocketAddr::from(([10, 0, 0, n], 443));
        let v6 = |n: u16| SocketAddr::from(([0xfd00, 0, 0, 0, 0, 0, 0, n], 443));
        assert_eq!(
            anyio_order(vec![v4(1), v4(2), v6(1), v4(3), v6(2)]),
            vec![v6(1), v4(3), v4(1), v4(2), v6(2)]
        );
        assert_eq!(anyio_order(vec![v4(1), v4(2)]), vec![v4(1), v4(2)]);
        assert_eq!(
            anyio_order(vec![v6(1), v6(2), v4(1)]),
            vec![v6(1), v4(1), v6(2)]
        );
    }

    /// The first address never answers (or is unroutable); the next attempt
    /// starts after the happy-eyeballs delay at the latest and wins.
    #[tokio::test]
    async fn unresponsive_first_address_falls_back_to_the_next() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let reachable = listener.local_addr().expect("addr");
        // TEST-NET-1 (RFC 5737): packets go nowhere, the connect hangs.
        let blackhole = SocketAddr::from(([192, 0, 2, 1], 9));
        let started = tokio::time::Instant::now();
        let stream = tokio::time::timeout(
            Duration::from_secs(5),
            connect_any(vec![blackhole, reachable]),
        )
        .await
        .expect("within the timeout")
        .expect("connected");
        assert_eq!(stream.peer_addr().expect("peer"), reachable);
        // Where TEST-NET-1 is unroutable the first attempt fails at once
        // instead of hanging; either way the second address wins quickly.
        assert!(started.elapsed() < Duration::from_secs(2));
    }

    #[tokio::test]
    async fn all_failed_attempts_are_one_connect_error() {
        let addr = {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
                .await
                .expect("bind");
            listener.local_addr().expect("addr")
        };
        let err = connect_any(vec![addr, addr]).await.expect_err("refused");
        assert_eq!(
            err,
            TransportError::new("ConnectError", "All connection attempts failed")
        );
    }

    #[test]
    fn os_errors_read_like_python() {
        let err = io::Error::from_raw_os_error(61);
        assert!(os_error_text(&err).starts_with("[Errno 61] "));
        assert!(!os_error_text(&err).contains("os error"));
    }
}
