//! The httpx 0.28 behaviour the HTTP family relies on (PLAN.md 5.5),
//! implemented on hyper's HTTP/1.1 connection API so that the request
//! target, the timeouts and the cookies are httpx's:
//!
//! - URLs are parsed and joined like httpx ([`crate::httpx_url`]); the
//!   request target is httpx's `raw_path`;
//! - httpx timeouts: `timeout=t` bounds connecting (DNS and TCP), the TLS
//!   handshake, and every single socket read and write separately; a
//!   response arriving in slow pieces is fine as long as no read waits
//!   longer than `t`;
//! - one [`HttpSession`] per batch with one cookie jar (httpx keeps one jar
//!   per `AsyncClient`, and the runner creates one client per batch); the
//!   `Cookie` header lists longer paths first like `http.cookiejar`;
//! - httpx's default headers, its User-Agent (Q2) and HTTP/1.1 only;
//! - redirects followed by hand like `AsyncClient._send_handling_redirects`
//!   (at most 20, each redirect body read and decoded before following,
//!   `Authorization` kept only on same-origin redirects and https upgrades,
//!   credentials in the URL sent as basic auth like `_build_request_auth`);
//! - `Content-Encoding` decoding as httpx ([`crate::decode`]).
//!
//! Every request uses its own connection (no pool). Proxy variables are not
//! read (none is set for the production agent).

use std::fmt;
use std::future::Future;
use std::io;
use std::net::SocketAddr;
use std::pin::Pin;
use std::sync::{Arc, Mutex, OnceLock};
use std::task::{Context, Poll};
use std::time::Duration;

use cookie_store::{CookieStore, RawCookie};
use http_body_util::{BodyExt, Empty};
use hyper::body::{Bytes, Incoming};
use hyper::header::{self, HeaderMap, HeaderName, HeaderValue};
use hyper_util::rt::TokioIo;
use num_bigint::BigInt;
use rustls::{ClientConfig, RootCertStore};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::net::TcpStream;
use tokio::task::JoinHandle;
use tokio::time::Sleep;

use crate::decode::{BodyDecoder, DecodeError};
use crate::happy_eyeballs;
use crate::httpx_url::HttpxUrl;

/// httpx's `USER_AGENT` (PLAN.md Q2: kept for zero-change parity).
pub const USER_AGENT: &str = "python-httpx/0.28.1";
/// `DEFAULT_MAX_REDIRECTS`.
pub const MAX_REDIRECTS: usize = 20;

/// The httpx exception family of a failed request.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HttpErrorKind {
    /// `httpx.TimeoutException` (connect, read or write).
    Timeout,
    /// `httpx.ConnectError`.
    Connect,
    /// Any other `httpx.RequestError` (protocol errors, decoding errors,
    /// `TooManyRedirects`, `UnsupportedProtocol`).
    Request,
    /// An exception outside the `RequestError` family (`InvalidURL`, ...).
    Unexpected,
}

/// A failed request: the httpx exception family and a P2 message.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HttpError {
    pub kind: HttpErrorKind,
    pub message: String,
}

impl HttpError {
    pub fn new(kind: HttpErrorKind, message: impl Into<String>) -> Self {
        HttpError {
            kind,
            message: message.into(),
        }
    }

    fn request(message: impl Into<String>) -> Self {
        HttpError::new(HttpErrorKind::Request, message)
    }

    /// str(httpx.ReadTimeout()) / str(httpx.ConnectTimeout()) is empty.
    fn timeout() -> Self {
        HttpError::new(HttpErrorKind::Timeout, "")
    }

    /// A transport error after the connection was made: a timed-out read
    /// or write is a timeout, anything else a protocol error.
    fn transport(err: &(dyn std::error::Error + 'static)) -> Self {
        let mut current = Some(err);
        while let Some(error) = current {
            if let Some(io) = error.downcast_ref::<io::Error>()
                && io.kind() == io::ErrorKind::TimedOut
            {
                return HttpError::timeout();
            }
            current = error.source();
        }
        HttpError::request(err.to_string())
    }
}

impl fmt::Display for HttpError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for HttpError {}

/// The text of a header as httpx decodes it: the whole header block as
/// ASCII, else UTF-8, else ISO-8859-1 (`Headers.encoding`); repeated headers
/// joined with `", "` (`Headers.__getitem__`).
pub fn header_text(headers: &HeaderMap, name: HeaderName) -> Option<String> {
    let values: Vec<&HeaderValue> = headers.get_all(&name).iter().collect();
    if values.is_empty() {
        return None;
    }
    let all_utf8 = headers
        .values()
        .all(|value| std::str::from_utf8(value.as_bytes()).is_ok());
    let decode = |value: &HeaderValue| -> String {
        let bytes = value.as_bytes();
        if all_utf8 {
            String::from_utf8_lossy(bytes).into_owned()
        } else {
            bytes.iter().map(|b| char::from(*b)).collect()
        }
    };
    Some(
        values
            .into_iter()
            .map(decode)
            .collect::<Vec<_>>()
            .join(", "),
    )
}

/// The comma-split `Content-Encoding` values (`get_list(split_commas=True)`).
pub fn content_encodings(headers: &HeaderMap) -> Vec<String> {
    header_text(headers, header::CONTENT_ENCODING)
        .map(|text| text.split(',').map(|v| v.trim().to_owned()).collect())
        .unwrap_or_default()
}

/// httpx `_same_origin`.
pub fn same_origin(a: &HttpxUrl, b: &HttpxUrl) -> bool {
    a.scheme == b.scheme && a.host == b.host && a.port_or_default() == b.port_or_default()
}

/// httpx `_is_https_redirect`: `http://host[:80]` to `https://host[:443]`.
pub fn is_https_redirect(from: &HttpxUrl, to: &HttpxUrl) -> bool {
    from.host == to.host
        && from.scheme == "http"
        && from.port_or_default() == Some(80)
        && to.scheme == "https"
        && to.port_or_default() == Some(443)
}

/// Whether a redirect from `from` to `to` keeps the `Authorization` header
/// (`_redirect_headers`).
pub fn keeps_authorization(from: &HttpxUrl, to: &HttpxUrl) -> bool {
    same_origin(from, to) || is_https_redirect(from, to)
}

/// `Response.has_redirect_location` statuses.
pub fn is_redirect_status(status: u16) -> bool {
    matches!(status, 301 | 302 | 303 | 307 | 308)
}

/// `_redirect_url`: the `Location` resolved against the request URL.
///
/// # Errors
/// `RemoteProtocolError` (a `RequestError`) for a `Location` httpx cannot
/// parse; `InvalidURL` (not a `RequestError`) when the resolved URL is
/// invalid.
pub fn redirect_url(request_url: &HttpxUrl, location: &str) -> Result<HttpxUrl, HttpError> {
    let unexpected =
        |err: crate::httpx_url::InvalidUrl| HttpError::new(HttpErrorKind::Unexpected, err.0);
    let mut url = HttpxUrl::parse(location)
        .map_err(|err| HttpError::request(format!("Invalid URL in location header: {err}.")))?;
    // An "absolute" Location without a host takes the request's host
    // (httpx issue 771).
    if !url.scheme.is_empty() && url.host.is_empty() {
        url = url.with_host(&request_url.host).map_err(unexpected)?;
    }
    if url.is_relative() {
        url = request_url.join(&url).map_err(unexpected)?;
    }
    if request_url
        .fragment
        .as_deref()
        .is_some_and(|f| !f.is_empty())
        && url.fragment.as_deref().is_none_or(str::is_empty)
    {
        url.fragment = request_url.fragment.clone();
    }
    Ok(url)
}

/// Parse a request URL like httpx (`URL(url)`).
///
/// # Errors
/// `InvalidURL` (not a `RequestError`).
pub fn request_url(url: &str) -> Result<HttpxUrl, HttpError> {
    HttpxUrl::parse(url).map_err(|err| HttpError::new(HttpErrorKind::Unexpected, err.0))
}

/// httpcore's scheme check when sending.
fn check_scheme(url: &HttpxUrl) -> Result<(), HttpError> {
    match url.scheme.as_str() {
        "" => Err(HttpError::request(
            "Request URL is missing an 'http://' or 'https://' protocol.",
        )),
        "http" | "https" => Ok(()),
        other => Err(HttpError::request(format!(
            "Request URL has an unsupported protocol '{other}://'."
        ))),
    }
}

/// `httpx.BasicAuth(username, password)`: the `Authorization` value.
pub fn basic_authorization(username: &str, password: &str) -> HeaderValue {
    use base64::Engine as _;
    let token = base64::engine::general_purpose::STANDARD.encode(format!("{username}:{password}"));
    HeaderValue::from_str(&format!("Basic {token}")).expect("base64 is a valid header value")
}

/// The Mozilla root store of `certifi` (webpki-roots), loaded once.
pub fn webpki_roots() -> Arc<RootCertStore> {
    static ROOTS: OnceLock<Arc<RootCertStore>> = OnceLock::new();
    ROOTS
        .get_or_init(|| {
            Arc::new(RootCertStore {
                roots: webpki_roots::TLS_SERVER_ROOTS.to_vec(),
            })
        })
        .clone()
}

/// One GET as the HTTP-family executors issue it.
#[derive(Clone, Debug)]
pub struct GetRequest<'a> {
    pub url: &'a str,
    /// `None`: no timeout (an infinite or huge Python timeout).
    pub timeout: Option<Duration>,
    /// The Python timeout is an integer beyond float range: httpcore raises
    /// `OverflowError` (not a `RequestError`) once it uses the timeout,
    /// after the URL and scheme checks and before connecting.
    pub timeout_overflows: bool,
    pub follow_redirects: bool,
    /// The `auth=` of the request; sent on the first request and kept on
    /// redirects per [`keeps_authorization`]. Without it, credentials in
    /// the URL are used.
    pub authorization: Option<HeaderValue>,
}

/// Seconds as a timeout; `None` (no timeout) for values a `Duration` cannot
/// hold (infinite, NaN, or huge).
pub fn timeout_duration(seconds: f64) -> Option<Duration> {
    Duration::try_from_secs_f64(seconds).ok()
}

/// A socket whose every read and write is bounded by the timeout, like
/// httpcore's per-operation `read`/`write` timeouts.
struct TimeoutIo<S> {
    inner: S,
    timeout: Option<Duration>,
    read_timer: Option<Pin<Box<Sleep>>>,
    write_timer: Option<Pin<Box<Sleep>>>,
}

fn poll_timer(
    timer: &mut Option<Pin<Box<Sleep>>>,
    timeout: Option<Duration>,
    cx: &mut Context<'_>,
) -> Poll<io::Error> {
    let Some(timeout) = timeout else {
        return Poll::Pending;
    };
    let sleep = timer.get_or_insert_with(|| Box::pin(tokio::time::sleep(timeout)));
    match sleep.as_mut().poll(cx) {
        Poll::Ready(()) => {
            *timer = None;
            Poll::Ready(io::Error::new(io::ErrorKind::TimedOut, "timed out"))
        }
        Poll::Pending => Poll::Pending,
    }
}

impl<S: AsyncRead + Unpin> AsyncRead for TimeoutIo<S> {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let this = &mut *self;
        match Pin::new(&mut this.inner).poll_read(cx, buf) {
            Poll::Ready(result) => {
                this.read_timer = None;
                Poll::Ready(result)
            }
            Poll::Pending => poll_timer(&mut this.read_timer, this.timeout, cx).map(Err),
        }
    }
}

impl<S: AsyncWrite + Unpin> AsyncWrite for TimeoutIo<S> {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        data: &[u8],
    ) -> Poll<io::Result<usize>> {
        let this = &mut *self;
        match Pin::new(&mut this.inner).poll_write(cx, data) {
            Poll::Ready(result) => {
                this.write_timer = None;
                Poll::Ready(result)
            }
            Poll::Pending => poll_timer(&mut this.write_timer, this.timeout, cx).map(Err),
        }
    }

    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let this = &mut *self;
        match Pin::new(&mut this.inner).poll_flush(cx) {
            Poll::Ready(result) => {
                this.write_timer = None;
                Poll::Ready(result)
            }
            Poll::Pending => poll_timer(&mut this.write_timer, this.timeout, cx).map(Err),
        }
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.inner).poll_shutdown(cx)
    }
}

/// `tokio::time::timeout` when a timeout is set.
async fn bounded<F: Future>(timeout: Option<Duration>, future: F) -> Result<F::Output, HttpError> {
    match timeout {
        Some(timeout) => tokio::time::timeout(timeout, future)
            .await
            .map_err(|_| HttpError::timeout()),
        None => Ok(future.await),
    }
}

/// A response with its body unread. Dropping it closes the connection.
pub struct HttpResponse {
    pub status: u16,
    pub headers: HeaderMap,
    body: Incoming,
    connection: JoinHandle<()>,
}

impl fmt::Debug for HttpResponse {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("HttpResponse")
            .field("status", &self.status)
            .field("headers", &self.headers)
            .finish_non_exhaustive()
    }
}

impl Drop for HttpResponse {
    fn drop(&mut self) {
        self.connection.abort();
    }
}

/// The HTTP state of one batch (one httpx `AsyncClient`).
pub struct HttpSession {
    jar: Mutex<CookieStore>,
    tls: tokio_rustls::TlsConnector,
    /// Tests: route host names to local addresses (the URL's port applies).
    overrides: Vec<(String, std::net::IpAddr)>,
}

impl Default for HttpSession {
    fn default() -> Self {
        HttpSession::new()
    }
}

impl HttpSession {
    /// A session trusting the Mozilla roots (httpx uses `certifi`).
    pub fn new() -> Self {
        HttpSession::with_roots(webpki_roots())
    }

    /// A session trusting `roots` (tests).
    pub fn with_roots(roots: Arc<RootCertStore>) -> Self {
        let mut tls: ClientConfig = (*crate::tls::verifying_client_config(roots)).clone();
        tls.alpn_protocols = vec![b"http/1.1".to_vec()];
        HttpSession {
            jar: Mutex::new(CookieStore::new()),
            tls: tokio_rustls::TlsConnector::from(Arc::new(tls)),
            overrides: Vec::new(),
        }
    }

    /// Resolve `host` to the address of `addr` (tests).
    pub fn resolve(mut self, host: &str, addr: SocketAddr) -> Self {
        self.overrides.push((host.to_owned(), addr.ip()));
        self
    }

    /// anyio's `connect_tcp` (resolution, address order, Happy Eyeballs).
    async fn connect_tcp(&self, host: &str, port: u16) -> Result<TcpStream, HttpError> {
        let addrs: Vec<SocketAddr> = match self.overrides.iter().find(|(h, _)| h == host) {
            Some((_, ip)) => vec![SocketAddr::new(*ip, port)],
            None => {
                let resolved: Vec<SocketAddr> = tokio::net::lookup_host((host, port))
                    .await
                    .map_err(|err| HttpError::new(HttpErrorKind::Connect, err.to_string()))?
                    .collect();
                happy_eyeballs::order_addresses(&resolved)
            }
        };
        happy_eyeballs::connect_addresses(addrs).await.map_err(|_| {
            HttpError::new(HttpErrorKind::Connect, happy_eyeballs::ALL_ATTEMPTS_FAILED)
        })
    }

    fn cookie_header(&self, url: &HttpxUrl) -> Option<HeaderValue> {
        let parsed = cookie_url(url)?;
        let jar = self.jar.lock().unwrap_or_else(|e| e.into_inner());
        let mut cookies = jar.matches(&parsed);
        if cookies.is_empty() {
            return None;
        }
        // http.cookiejar: longer paths first, otherwise insertion order.
        cookies.sort_by_key(|cookie| std::cmp::Reverse(String::from(&cookie.path).len()));
        let text = cookies
            .iter()
            .map(|c| format!("{}={}", c.name(), c.value()))
            .collect::<Vec<_>>()
            .join("; ");
        HeaderValue::from_str(&text).ok()
    }

    fn store_cookies(&self, url: &HttpxUrl, headers: &HeaderMap) {
        let Some(parsed) = cookie_url(url) else {
            return;
        };
        let cookies: Vec<RawCookie<'static>> = headers
            .get_all(header::SET_COOKIE)
            .iter()
            .filter_map(|value| value.to_str().ok())
            .filter_map(|text| RawCookie::parse(text.to_owned()).ok())
            .map(|mut cookie| {
                if let Some(path) = cookie.path().map(cookie_path) {
                    cookie.set_path(path);
                }
                cookie
            })
            .collect();
        if !cookies.is_empty() {
            let mut jar = self.jar.lock().unwrap_or_else(|e| e.into_inner());
            jar.store_response_cookies(cookies.into_iter(), &parsed);
        }
    }

    /// Send one GET without following redirects.
    ///
    /// # Errors
    /// The classified transport error.
    pub async fn send_once(
        &self,
        url: &HttpxUrl,
        timeout: Option<Duration>,
        authorization: Option<&HeaderValue>,
        extra_headers: &[(HeaderName, HeaderValue)],
    ) -> Result<HttpResponse, HttpError> {
        check_scheme(url)?;
        if url.host.is_empty() {
            return Err(HttpError::request("Request URL is missing a host."));
        }
        let port = url
            .port_or_default()
            .and_then(|p| u16::try_from(p).ok())
            .ok_or_else(|| HttpError::new(HttpErrorKind::Unexpected, "port must be 0-65535."))?;
        let target = url.raw_path();
        let mut request = hyper::Request::builder()
            .method(hyper::Method::GET)
            .uri(target.as_str())
            .header(header::HOST, url.netloc())
            .header(header::ACCEPT, "*/*")
            .header(header::ACCEPT_ENCODING, "gzip, deflate")
            .header(header::CONNECTION, "keep-alive")
            .header(header::USER_AGENT, USER_AGENT)
            .body(Empty::<Bytes>::new())
            .map_err(|err| HttpError::new(HttpErrorKind::Unexpected, err.to_string()))?;
        let headers = request.headers_mut();
        for (name, value) in extra_headers {
            headers.insert(name.clone(), value.clone());
        }
        if let Some(value) = authorization {
            headers.insert(header::AUTHORIZATION, value.clone());
        }
        if let Some(value) = self.cookie_header(url) {
            headers.insert(header::COOKIE, value);
        }

        let tcp = bounded(timeout, self.connect_tcp(&url.host, port)).await??;
        let _ = tcp.set_nodelay(true);
        let (mut sender, connection) = if url.scheme == "https" {
            let name = crate::tls::server_name(&url.host)
                .map_err(|err| HttpError::new(HttpErrorKind::Connect, err.0))?;
            let tls = bounded(timeout, self.tls.connect(name, tcp))
                .await?
                .map_err(|err| HttpError::new(HttpErrorKind::Connect, err.to_string()))?;
            handshake(tls, timeout).await?
        } else {
            handshake(tcp, timeout).await?
        };
        let response = match sender.send_request(request).await {
            Ok(response) => response,
            Err(err) => {
                connection.abort();
                return Err(HttpError::transport(&err));
            }
        };
        let (parts, body) = response.into_parts();
        self.store_cookies(url, &parts.headers);
        Ok(HttpResponse {
            status: parts.status.as_u16(),
            headers: parts.headers,
            body,
            connection,
        })
    }

    /// `client.stream("GET", url, ...)`: the final response with its body
    /// unread, after following redirects when asked to.
    ///
    /// # Errors
    /// The classified transport error, `TooManyRedirects` and invalid
    /// `Location` headers as `Request` errors, invalid URLs as `Unexpected`.
    pub async fn get(&self, request: &GetRequest<'_>) -> Result<HttpResponse, HttpError> {
        let mut url = request_url(request.url)?;
        if request.timeout_overflows {
            check_scheme(&url)?;
            return Err(HttpError::new(
                HttpErrorKind::Unexpected,
                "int too large to convert to float",
            ));
        }
        let mut authorization = request.authorization.clone();
        if authorization.is_none() && !url.userinfo.is_empty() {
            authorization = Some(basic_authorization(&url.username(), &url.password()));
        }
        let mut redirects = 0;
        loop {
            let response = self
                .send_once(&url, request.timeout, authorization.as_ref(), &[])
                .await?;
            let Some(next) = redirect_target(&url, &response)? else {
                return Ok(response);
            };
            if !request.follow_redirects {
                return Ok(response);
            }
            redirects += 1;
            read_body(response, None).await.map_err(|err| match err {
                BodyError::Http(err) => err,
                BodyError::TooLarge(message) => HttpError::request(message),
            })?;
            if redirects > MAX_REDIRECTS {
                return Err(HttpError::request("Exceeded maximum allowed redirects."));
            }
            if !keeps_authorization(&url, &next) {
                authorization = None;
            }
            url = next;
        }
    }
}

/// `http.cookiejar.escape_path`: `urllib.parse.quote(path, HTTP_PATH_SAFE)`
/// (existing `%` kept) with every `%xx` escape upper-cased.
fn escape_path(path: &str) -> String {
    const SAFE: &str = "_.-~%/;:@&=+$,!~*'()";
    let mut quoted = String::with_capacity(path.len());
    for c in path.chars() {
        if c.is_ascii_alphanumeric() || SAFE.contains(c) {
            quoted.push(c);
        } else {
            let mut buf = [0u8; 4];
            for byte in c.encode_utf8(&mut buf).bytes() {
                quoted.push_str(&format!("%{byte:02X}"));
            }
        }
    }
    let bytes = quoted.as_bytes();
    let mut out = String::with_capacity(quoted.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%'
            && bytes.get(i + 1).is_some_and(u8::is_ascii_hexdigit)
            && bytes.get(i + 2).is_some_and(u8::is_ascii_hexdigit)
        {
            out.push('%');
            out.push_str(&quoted[i + 1..i + 3].to_ascii_uppercase());
            i += 3;
        } else {
            out.push(char::from(bytes[i]));
            i += 1;
        }
    }
    out
}

/// A path as the cookie jar sees it. `http.cookiejar` compares request and
/// cookie paths as strings after [`escape_path`], while `url::Url` would
/// apply WHATWG normalisation (`%2e%2e` segments). After `escape_path` the
/// path is ASCII without backslashes; escaping every `%` as `%25` then
/// keeps distinct paths distinct and leaves nothing for WHATWG to
/// normalise. Cookie paths from `Set-Cookie` get the same mapping.
fn cookie_path(path: &str) -> String {
    escape_path(path).replace('%', "%25")
}

/// The URL the cookie jar matches against (path mapped by [`cookie_path`]).
fn cookie_url(url: &HttpxUrl) -> Option<url::Url> {
    let mut copy = url.clone();
    copy.path = cookie_path(&url.path);
    copy.query = None;
    copy.fragment = None;
    url::Url::parse(&copy.to_string()).ok()
}

type Sender = hyper::client::conn::http1::SendRequest<Empty<Bytes>>;

async fn handshake<S>(
    stream: S,
    timeout: Option<Duration>,
) -> Result<(Sender, JoinHandle<()>), HttpError>
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let io = TokioIo::new(TimeoutIo {
        inner: stream,
        timeout,
        read_timer: None,
        write_timer: None,
    });
    let (sender, connection) = hyper::client::conn::http1::handshake(io)
        .await
        .map_err(|err| HttpError::transport(&err))?;
    let task = tokio::spawn(async move {
        let _ = connection.await;
    });
    Ok((sender, task))
}

/// The redirect a response asks for: `None` unless the status is a redirect
/// with a `Location` header. httpx builds the redirect request even when it
/// does not follow it, so an invalid `Location` fails either way.
///
/// # Errors
/// An invalid `Location`.
pub fn redirect_target(
    url: &HttpxUrl,
    response: &HttpResponse,
) -> Result<Option<HttpxUrl>, HttpError> {
    if !is_redirect_status(response.status) {
        return Ok(None);
    }
    match header_text(&response.headers, header::LOCATION) {
        Some(location) => redirect_url(url, &location).map(Some),
        None => Ok(None),
    }
}

/// Why reading a body stopped.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BodyError {
    /// The body passed the [`BodyLimit`] (`BodyTooLargeError`, its message).
    TooLarge(String),
    Http(HttpError),
}

/// A cap on the decoded body (json-metrics `max_body_bytes`).
#[derive(Clone, Debug)]
pub struct BodyLimit<'a> {
    pub limit: &'a BigInt,
}

fn decode_error(err: DecodeError) -> BodyError {
    BodyError::Http(HttpError::request(err.0))
}

/// Read and decode a body (`aiter_bytes`), failing once the decoded bytes
/// pass `limit` (json-metrics `_fetch`: a declared `Content-Length` of all
/// ASCII digits over the limit fails before reading). Without a limit the
/// body is read and discarded (redirect bodies).
///
/// # Errors
/// [`BodyError::TooLarge`] with the Python message, or the transport or
/// decoding error.
pub async fn read_body(
    mut response: HttpResponse,
    limit: Option<BodyLimit<'_>>,
) -> Result<Vec<u8>, BodyError> {
    if let Some(BodyLimit { limit }) = &limit
        && let Some(declared) = header_text(&response.headers, header::CONTENT_LENGTH)
        && !declared.is_empty()
        && declared.bytes().all(|b| b.is_ascii_digit())
        && declared.parse::<BigInt>().is_ok_and(|n| &n > *limit)
    {
        return Err(BodyError::TooLarge(format!(
            "response body of {declared} bytes exceeds the {limit} byte limit"
        )));
    }
    let mut decoder = BodyDecoder::new(
        content_encodings(&response.headers)
            .iter()
            .map(String::as_str),
    );
    let mut body: Vec<u8> = Vec::new();
    let push = |body: &mut Vec<u8>, chunk: Vec<u8>| -> Result<(), BodyError> {
        if let Some(BodyLimit { limit }) = &limit {
            if BigInt::from(body.len() + chunk.len()) > **limit {
                return Err(BodyError::TooLarge(format!(
                    "response body exceeds the {limit} byte limit"
                )));
            }
            body.extend_from_slice(&chunk);
        }
        Ok(())
    };
    while let Some(frame) = response.body.frame().await {
        let frame = frame.map_err(|err| BodyError::Http(HttpError::transport(&err)))?;
        if let Ok(data) = frame.into_data() {
            let decoded = decoder.decode(&data).map_err(decode_error)?;
            push(&mut body, decoded)?;
        }
    }
    let tail = decoder.flush().map_err(decode_error)?;
    push(&mut body, tail)?;
    Ok(body)
}

#[cfg(test)]
mod tests;
