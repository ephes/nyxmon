//! A scripted local HTTP/1.1 server standing in for Telegram and OpsGate.

use std::collections::{BTreeMap, VecDeque};
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

/// A scripted response.
#[derive(Clone, Debug)]
pub(crate) enum Reply {
    Respond {
        status: u16,
        body: String,
        location: Option<String>,
    },
    /// Accept the request and never answer.
    Stall,
    /// Announce a longer body than is sent, then close.
    ShortBody,
    /// A 200 response written in small pieces with `gap` between them.
    Dribble { gap: std::time::Duration },
}

/// One request as the server received it.
#[derive(Clone, Debug)]
pub(crate) struct Recorded {
    pub kind: &'static str,
    pub method: String,
    pub path: String,
    pub headers: BTreeMap<String, String>,
    pub body: String,
}

pub(crate) struct FakeServer {
    pub addr: SocketAddr,
    pub requests: Arc<Mutex<Vec<Recorded>>>,
    queues: Arc<Mutex<BTreeMap<&'static str, VecDeque<Reply>>>>,
}

impl FakeServer {
    /// Serve `telegram` replies for `/bot…` paths and `opsgate` replies for
    /// every other path, in order.
    pub async fn start(telegram: Vec<Reply>, opsgate: Vec<Reply>) -> FakeServer {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let addr = listener.local_addr().expect("addr");
        let requests = Arc::new(Mutex::new(Vec::new()));
        let queues = Arc::new(Mutex::new(BTreeMap::from([
            ("telegram", VecDeque::from(telegram)),
            ("opsgate", VecDeque::from(opsgate)),
        ])));
        let (recorded, scripted) = (requests.clone(), queues.clone());
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                tokio::spawn(serve(stream, recorded.clone(), scripted.clone()));
            }
        });
        FakeServer {
            addr,
            requests,
            queues,
        }
    }

    pub fn base(&self) -> String {
        format!("http://{}", self.addr)
    }

    pub fn requests(&self) -> Vec<Recorded> {
        self.requests.lock().expect("lock").clone()
    }

    /// Replies that were never requested.
    pub fn unused(&self) -> usize {
        self.queues
            .lock()
            .expect("lock")
            .values()
            .map(VecDeque::len)
            .sum()
    }
}

async fn serve(
    mut stream: TcpStream,
    requests: Arc<Mutex<Vec<Recorded>>>,
    queues: Arc<Mutex<BTreeMap<&'static str, VecDeque<Reply>>>>,
) {
    let mut buffer = Vec::new();
    let head_end = loop {
        if let Some(pos) = buffer.windows(4).position(|w| w == b"\r\n\r\n") {
            break pos + 4;
        }
        let mut chunk = [0u8; 4096];
        match stream.read(&mut chunk).await {
            Ok(0) | Err(_) => return,
            Ok(n) => buffer.extend_from_slice(&chunk[..n]),
        }
    };
    let head = String::from_utf8_lossy(&buffer[..head_end]).into_owned();
    let mut lines = head.split("\r\n");
    let request_line = lines.next().unwrap_or_default();
    let mut parts = request_line.split(' ');
    let method = parts.next().unwrap_or_default().to_owned();
    let path = parts.next().unwrap_or_default().to_owned();
    let headers: BTreeMap<String, String> = lines
        .filter_map(|line| line.split_once(':'))
        .map(|(name, value)| (name.trim().to_ascii_lowercase(), value.trim().to_owned()))
        .collect();
    let length: usize = headers
        .get("content-length")
        .and_then(|v| v.parse().ok())
        .unwrap_or(0);
    while buffer.len() < head_end + length {
        let mut chunk = [0u8; 4096];
        match stream.read(&mut chunk).await {
            Ok(0) | Err(_) => return,
            Ok(n) => buffer.extend_from_slice(&chunk[..n]),
        }
    }
    let body = String::from_utf8(buffer[head_end..head_end + length].to_vec()).expect("UTF-8 body");
    let kind = if path.starts_with("/bot") {
        "telegram"
    } else {
        "opsgate"
    };
    requests.lock().expect("lock").push(Recorded {
        kind,
        method,
        path,
        headers,
        body,
    });
    let reply = queues
        .lock()
        .expect("lock")
        .get_mut(kind)
        .and_then(VecDeque::pop_front);
    let response = match reply {
        None => panic!("unexpected {kind} request"),
        Some(Reply::Stall) => {
            tokio::time::sleep(std::time::Duration::from_secs(3600)).await;
            return;
        }
        Some(Reply::Dribble { gap }) => {
            let response = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\
                            Content-Length: 11\r\nConnection: close\r\n\r\n{\"ok\":true}";
            for piece in response.as_bytes().chunks(16) {
                tokio::time::sleep(gap).await;
                if stream.write_all(piece).await.is_err() {
                    return;
                }
            }
            let _ = stream.shutdown().await;
            return;
        }
        Some(Reply::ShortBody) => {
            "HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{\"ok\"".to_owned()
        }
        Some(Reply::Respond {
            status,
            body,
            location,
        }) => {
            let location = location.map_or_else(String::new, |l| format!("Location: {l}\r\n"));
            format!(
                "HTTP/1.1 {status} Scripted\r\nContent-Type: application/json\r\n{location}\
                 Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
        }
    };
    let _ = stream.write_all(response.as_bytes()).await;
    let _ = stream.shutdown().await;
}
