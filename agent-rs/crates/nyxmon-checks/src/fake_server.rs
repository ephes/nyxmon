//! Scripted line-protocol servers on `127.0.0.1` for the mail executor
//! tests, and a recording [`MailEnv`].

use std::net::{IpAddr, Ipv4Addr};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use nyxmon_core::log::{LogSink, RecordingSink};
use rcgen::{CertificateParams, KeyPair, SanType};
use rustls::ServerConfig;
use rustls::pki_types::{PrivateKeyDer, PrivatePkcs8KeyDer};
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::net::TcpListener;
use tokio::task::JoinHandle;
use tokio_rustls::TlsAcceptor;

use crate::mail::MailEnv;
use crate::tls::crypto_provider;

/// One step of a server script.
#[derive(Clone, Debug)]
pub enum Step {
    /// Write these bytes.
    Send(String),
    /// Read one line and record it (without the line ending).
    Recv,
    /// Read a DATA body up to the `.` line and record it as one entry.
    RecvData,
    /// Switch the connection to TLS (server side of STARTTLS).
    StartTls,
    /// Close the connection.
    Close,
    /// Stop answering; keep the connection open until the client leaves.
    Stall,
}

pub fn send(text: &str) -> Step {
    Step::Send(text.to_owned())
}

trait Io: AsyncRead + AsyncWrite + Unpin + Send {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> Io for T {}

/// A self-signed certificate for `localhost` and `127.0.0.1`.
pub fn tls_server_config() -> Arc<ServerConfig> {
    let key = KeyPair::generate().unwrap();
    let mut params = CertificateParams::new(vec!["localhost".to_owned()]).unwrap();
    params
        .subject_alt_names
        .push(SanType::IpAddress(IpAddr::V4(Ipv4Addr::LOCALHOST)));
    let cert = params.self_signed(&key).unwrap();
    let key = PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(key.serialize_der()));
    Arc::new(
        ServerConfig::builder_with_provider(crypto_provider())
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_no_client_auth()
            .with_single_cert(vec![cert.der().clone()], key)
            .unwrap(),
    )
}

/// A server that serves one script per accepted connection, in order.
pub struct ScriptServer {
    pub port: u16,
    handle: JoinHandle<Vec<Vec<String>>>,
}

impl ScriptServer {
    /// `implicit_tls` wraps every connection in TLS before the script.
    pub async fn start(scripts: Vec<Vec<Step>>, implicit_tls: bool) -> ScriptServer {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let acceptor = TlsAcceptor::from(tls_server_config());
        let handle = tokio::spawn(async move {
            let mut transcripts = Vec::new();
            for script in scripts {
                let (socket, _) = listener.accept().await.unwrap();
                let stream: Box<dyn Io> = if implicit_tls {
                    match acceptor.accept(socket).await {
                        Ok(tls) => Box::new(tls),
                        Err(_) => {
                            transcripts.push(vec!["<handshake failed>".to_owned()]);
                            continue;
                        }
                    }
                } else {
                    Box::new(socket)
                };
                transcripts.push(run_script(stream, script, &acceptor).await);
            }
            transcripts
        });
        ScriptServer { port, handle }
    }

    /// The lines each connection received.
    pub async fn transcripts(self) -> Vec<Vec<String>> {
        tokio::time::timeout(Duration::from_secs(10), self.handle)
            .await
            .expect("server finished")
            .expect("server task")
    }
}

async fn run_script(stream: Box<dyn Io>, script: Vec<Step>, acceptor: &TlsAcceptor) -> Vec<String> {
    let mut reader = BufReader::new(stream);
    let mut seen = Vec::new();
    for step in script {
        match step {
            Step::Send(text) => {
                if reader.get_mut().write_all(text.as_bytes()).await.is_err() {
                    return seen;
                }
            }
            Step::Recv => match read_line(&mut reader).await {
                Some(line) => seen.push(line),
                None => return seen,
            },
            Step::RecvData => {
                let mut body = Vec::new();
                while let Some(line) = read_line(&mut reader).await {
                    if line == "." {
                        break;
                    }
                    body.push(line);
                }
                seen.push(format!("DATA:{}", body.join("\n")));
            }
            Step::StartTls => {
                let inner = reader.into_inner();
                match acceptor.accept(inner).await {
                    Ok(tls) => reader = BufReader::new(Box::new(tls) as Box<dyn Io>),
                    Err(_) => {
                        seen.push("<handshake failed>".to_owned());
                        return seen;
                    }
                }
            }
            Step::Close => return seen,
            Step::Stall => {
                while read_line(&mut reader).await.is_some() {}
                return seen;
            }
        }
    }
    // Record whatever else arrives until the client closes.
    while let Some(line) = read_line(&mut reader).await {
        seen.push(line);
    }
    seen
}

async fn read_line(reader: &mut BufReader<Box<dyn Io>>) -> Option<String> {
    let mut line = Vec::new();
    match reader.read_until(b'\n', &mut line).await {
        Ok(0) | Err(_) => None,
        Ok(_) => {
            while line.last().is_some_and(|b| *b == b'\n' || *b == b'\r') {
                line.pop();
            }
            Some(String::from_utf8_lossy(&line).into_owned())
        }
    }
}

/// A [`MailEnv`] with a fixed clock, token and EHLO name that records the
/// retry delays and warnings.
#[derive(Default)]
pub struct TestEnv {
    pub sleeps: Mutex<Vec<Duration>>,
    pub log: RecordingSink,
    pub now: f64,
}

impl TestEnv {
    pub fn new() -> TestEnv {
        TestEnv {
            now: 1_760_000_000.75,
            ..TestEnv::default()
        }
    }

    pub fn sleeps(&self) -> Vec<Duration> {
        self.sleeps.lock().unwrap().clone()
    }
}

impl MailEnv for TestEnv {
    fn now(&self) -> f64 {
        self.now
    }

    fn token(&self) -> String {
        "a1b2c3".to_owned()
    }

    fn message_id(&self, domain: &str) -> String {
        format!("<1.2.3@{domain}>")
    }

    async fn sleep(&self, delay: Duration) {
        self.sleeps.lock().unwrap().push(delay);
    }

    async fn local_hostname(&self) -> String {
        "client.example".to_owned()
    }

    fn log(&self) -> &dyn LogSink {
        &self.log
    }
}
