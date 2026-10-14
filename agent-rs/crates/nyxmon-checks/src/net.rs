//! Blocking-socket semantics of the Python mail clients on tokio:
//! `socket.create_connection` (sequential, one timeout per address), a
//! socket timeout per I/O operation, and `str(OSError)` texts.

use std::io;
use std::net::SocketAddr;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};
use std::time::Duration;

use rustls::ClientConfig;
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader, ReadBuf};
use tokio::net::TcpStream;
use tokio_rustls::TlsConnector;
use tokio_rustls::client::TlsStream;

use crate::tls;

/// Why connecting or a TLS handshake failed, in Python's exception classes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NetError {
    /// `TimeoutError` (`socket.timeout`); the text is `str(exc)`.
    Timeout(String),
    /// `ssl.SSLError` (and `ssl.SSLCertVerificationError`).
    Tls(String),
    /// Any other `OSError` (incl. `socket.gaierror`).
    Os(String),
    /// A non-`OSError` exception, e.g. the `UnicodeError` of an IDNA host.
    Other(String),
}

/// `str(exc)` of the `OSError` Python raises for `err`: `[Errno N] text`
/// for OS errors, `timed out` for timeouts.
pub fn os_error_text(err: &io::Error) -> String {
    if err.kind() == io::ErrorKind::TimedOut && err.raw_os_error().is_none() {
        return "timed out".to_owned();
    }
    match err.raw_os_error() {
        Some(code) => {
            let text = err.to_string();
            let text = text.split(" (os error").next().unwrap_or(&text).to_owned();
            format!("[Errno {code}] {text}")
        }
        None => err.to_string(),
    }
}

fn timed_out() -> io::Error {
    io::Error::new(io::ErrorKind::TimedOut, "timed out")
}

/// Run `fut` under a socket timeout.
pub async fn with_timeout<T>(
    timeout: Duration,
    fut: impl Future<Output = io::Result<T>>,
) -> io::Result<T> {
    tokio::time::timeout(timeout, fut)
        .await
        .unwrap_or_else(|_| Err(timed_out()))
}

/// `socket.getaddrinfo(host, port, 0, SOCK_STREAM)` (blocking libc call on
/// the blocking pool); the host is IDNA-encoded like Python's `socket`.
pub async fn resolve(host: &str, port: u16) -> Result<Vec<SocketAddr>, NetError> {
    let ascii = tls::idna_encode(host).map_err(|err| {
        NetError::Other(format!(
            "encoding with 'idna' codec failed (UnicodeError: {err})"
        ))
    })?;
    let lookup = tokio::task::spawn_blocking(move || {
        let hints = dns_lookup::AddrInfoHints {
            socktype: dns_lookup::SockType::Stream.into(),
            ..Default::default()
        };
        match dns_lookup::getaddrinfo(Some(&ascii), Some(&port.to_string()), Some(hints)) {
            Ok(iter) => Ok(iter
                .filter_map(Result::ok)
                .map(|info| info.sockaddr)
                .collect::<Vec<_>>()),
            Err(err) => {
                let code = err.error_num();
                let io_err = io::Error::from(err);
                if io_err.raw_os_error().is_some() {
                    return Err(os_error_text(&io_err));
                }
                let text = io_err.to_string();
                let detail = text
                    .strip_prefix("failed to lookup address information: ")
                    .unwrap_or(&text);
                Err(format!("[Errno {code}] {detail}"))
            }
        }
    })
    .await
    .map_err(|err| NetError::Other(err.to_string()))?;
    lookup.map_err(NetError::Os)
}

/// `socket.create_connection((host, port), timeout)`: every resolved
/// address in turn, each bounded by `timeout`; the last error wins.
pub async fn connect_tcp(host: &str, port: u16, timeout: Duration) -> Result<TcpStream, NetError> {
    let addrs = resolve(host, port).await?;
    let mut last = None;
    for addr in addrs {
        match with_timeout(timeout, TcpStream::connect(addr)).await {
            Ok(stream) => return Ok(stream),
            Err(err) => last = Some(err),
        }
    }
    Err(match last {
        Some(err) if err.kind() == io::ErrorKind::TimedOut => {
            NetError::Timeout(os_error_text(&err))
        }
        Some(err) => NetError::Os(os_error_text(&err)),
        None => NetError::Os("getaddrinfo returns an empty list".to_owned()),
    })
}

/// A plain or TLS connection.
pub enum MailStream {
    Plain(TcpStream),
    Tls(Box<TlsStream<TcpStream>>),
}

/// `context.wrap_socket(sock, server_hostname=host)` with the handshake
/// bounded by `timeout`.
pub async fn tls_handshake(
    tcp: TcpStream,
    host: &str,
    config: Arc<ClientConfig>,
    timeout: Duration,
) -> Result<MailStream, NetError> {
    let name = tls::server_name(host).map_err(|err| NetError::Other(err.to_string()))?;
    let connector = TlsConnector::from(config);
    match with_timeout(timeout, connector.connect(name, tcp)).await {
        Ok(stream) => Ok(MailStream::Tls(Box::new(stream))),
        Err(err) => Err(classify_tls_error(&err)),
    }
}

fn classify_tls_error(err: &io::Error) -> NetError {
    if let Some(inner) = err
        .get_ref()
        .and_then(|e| e.downcast_ref::<rustls::Error>())
    {
        return NetError::Tls(inner.to_string());
    }
    match err.kind() {
        io::ErrorKind::TimedOut => {
            NetError::Timeout("_ssl.c:1011: The handshake operation timed out".to_owned())
        }
        io::ErrorKind::UnexpectedEof => {
            NetError::Tls("EOF occurred in violation of protocol (_ssl.c:1000)".to_owned())
        }
        _ => NetError::Os(os_error_text(err)),
    }
}

impl AsyncRead for MailStream {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        match self.get_mut() {
            MailStream::Plain(s) => Pin::new(s).poll_read(cx, buf),
            MailStream::Tls(s) => Pin::new(s.as_mut()).poll_read(cx, buf),
        }
    }
}

impl AsyncWrite for MailStream {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        match self.get_mut() {
            MailStream::Plain(s) => Pin::new(s).poll_write(cx, buf),
            MailStream::Tls(s) => Pin::new(s.as_mut()).poll_write(cx, buf),
        }
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        match self.get_mut() {
            MailStream::Plain(s) => Pin::new(s).poll_flush(cx),
            MailStream::Tls(s) => Pin::new(s.as_mut()).poll_flush(cx),
        }
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        match self.get_mut() {
            MailStream::Plain(s) => Pin::new(s).poll_shutdown(cx),
            MailStream::Tls(s) => Pin::new(s.as_mut()).poll_shutdown(cx),
        }
    }
}

/// A buffered connection with Python's per-operation socket timeout.
pub struct Conn {
    reader: BufReader<MailStream>,
    timeout: Duration,
}

impl Conn {
    pub fn new(stream: MailStream, timeout: Duration) -> Self {
        Conn {
            reader: BufReader::new(stream),
            timeout,
        }
    }

    /// The stream, dropping buffered input (Python discards its
    /// `makefile` buffer when it wraps the socket for STARTTLS).
    pub fn into_stream(self) -> MailStream {
        self.reader.into_inner()
    }

    pub fn timeout(&self) -> Duration {
        self.timeout
    }

    /// `file.readline(limit)`: up to and including `\n`, at most `limit`
    /// bytes; empty at EOF. Every `recv` is bounded by the timeout. A TLS
    /// peer closing without `close_notify` reads as EOF (Python's
    /// `suppress_ragged_eofs`).
    pub async fn read_line(&mut self, limit: usize) -> io::Result<Vec<u8>> {
        let mut line = Vec::new();
        while line.len() < limit {
            let available = match with_timeout(self.timeout, self.reader.fill_buf()).await {
                Ok(buf) => buf,
                Err(err) if err.kind() == io::ErrorKind::UnexpectedEof => break,
                Err(err) => return Err(err),
            };
            if available.is_empty() {
                break;
            }
            let room = limit - line.len();
            let take = available.iter().take(room).position(|&b| b == b'\n');
            let (count, done) = match take {
                Some(index) => (index + 1, true),
                None => (available.len().min(room), false),
            };
            line.extend_from_slice(&available[..count]);
            self.reader.consume(count);
            if done {
                break;
            }
        }
        Ok(line)
    }

    /// `file.read(size)`: exactly `size` bytes unless EOF comes first.
    pub async fn read_exact_or_eof(&mut self, size: usize) -> io::Result<Vec<u8>> {
        let mut data = Vec::with_capacity(size.min(1 << 16));
        while data.len() < size {
            let available = match with_timeout(self.timeout, self.reader.fill_buf()).await {
                Ok(buf) => buf,
                Err(err) if err.kind() == io::ErrorKind::UnexpectedEof => break,
                Err(err) => return Err(err),
            };
            if available.is_empty() {
                break;
            }
            let count = available.len().min(size - data.len());
            data.extend_from_slice(&available[..count]);
            self.reader.consume(count);
        }
        Ok(data)
    }

    /// `sock.sendall(data)`: the whole write bounded by one timeout.
    pub async fn send_all(&mut self, data: &[u8]) -> io::Result<()> {
        let stream = self.reader.get_mut();
        with_timeout(self.timeout, async {
            stream.write_all(data).await?;
            stream.flush().await
        })
        .await
    }
}
