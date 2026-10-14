//! The `dns` executor (`executors/dns_executor.py`, PLAN.md 5.6) with a stub
//! resolver that follows dnspython 2.8's `asyncresolver.Resolver.resolve`:
//!
//! - `Resolver()` reads `/etc/resolv.conf` on every check (also when
//!   `dns_server` is set): `nameserver` lines, `options rotate` and
//!   `options edns0` (EDNS version 0, payload 1232; otherwise no EDNS);
//! - the query name is `dns.name.from_text(check.url)` made absolute (no
//!   search list); `timeout` and `lifetime` are the configured timeout;
//! - the `_Resolution` loop: nameservers in order with a 0.1 s backoff
//!   doubling to 2 s between rounds, every attempt bounded by the time left
//!   of the lifetime; a nameserver is dropped on socket errors, malformed
//!   TCP responses, truncated TCP responses and unwanted rcodes (SERVFAIL
//!   included), kept after a timeout; a truncated UDP response is retried
//!   once over TCP;
//! - UDP reads ignore datagrams from other addresses, unparseable messages
//!   and messages that are not a response to the query
//!   (`ignore_unexpected`/`ignore_errors`);
//! - the answer follows CNAMEs in the answer section
//!   (`resolve_chaining`); records are rendered as dnspython `to_text`.

pub mod wire;

use std::collections::VecDeque;
use std::hash::{BuildHasher, Hasher};
use std::net::{IpAddr, SocketAddr};
use std::path::PathBuf;
use std::time::{Duration, Instant};

use nyxmon_core::config::dns::{DnsConfig, DnsConfigError, check_ip_match};
use nyxmon_core::model::{Check, CheckResult, ResultStatus};
use nyxmon_core::pyjson::{PyDict, PyJson, py_str, py_truthy};
use nyxmon_core::pyurl::py_ip_address;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpSocket, UdpSocket};

use crate::pyos;
use wire::{Message, Name, Partial, Question};

/// `dns.message.DEFAULT_EDNS_PAYLOAD`.
const EDNS_PAYLOAD: u16 = 1232;

/// Where the resolver reads its system configuration and which port it
/// queries (tests point both at fakes).
#[derive(Clone, Debug)]
pub struct DnsSettings {
    pub resolv_conf: PathBuf,
    pub port: u16,
}

impl Default for DnsSettings {
    fn default() -> Self {
        DnsSettings {
            resolv_conf: PathBuf::from("/etc/resolv.conf"),
            port: 53,
        }
    }
}

/// Why a resolution failed, as the executor classifies dnspython's
/// exceptions.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ResolveError {
    NxDomain,
    /// `dns.resolver.Timeout` (`LifetimeTimeout`).
    Timeout,
    NoAnswer,
    /// A `ValueError`: `configuration_error` with this message.
    Value(String),
    /// Any other exception: `unexpected_error` with `str(exc)`.
    Other(String),
}

/// The resolver configuration `Resolver()` builds.
#[derive(Clone, Debug, Default)]
struct SystemConfig {
    nameservers: Vec<String>,
    rotate: bool,
    edns: bool,
}

fn not_a_nameserver(text: &str) -> ResolveError {
    ResolveError::Value(format!(
        "nameserver {text} is not a dns.nameserver.Nameserver instance or text form, IP address, nor a valid https URL"
    ))
}

/// `BaseResolver.read_resolv_conf` (the parts that can change a query).
/// `domain`/`search` lines are not used without a search list and are
/// skipped.
fn read_resolv_conf(settings: &DnsSettings) -> Result<SystemConfig, ResolveError> {
    let path = &settings.resolv_conf;
    let bytes = std::fs::read(path)
        .map_err(|_| ResolveError::Other(format!("cannot open {}", path.display())))?;
    let text = String::from_utf8_lossy(&bytes);
    let mut config = SystemConfig::default();
    for line in text.split_inclusive('\n') {
        if line.starts_with('#') || line.starts_with(';') {
            continue;
        }
        let tokens: Vec<&str> = line.split_whitespace().collect();
        if tokens.len() < 2 {
            continue;
        }
        match tokens[0] {
            "nameserver" => config.nameservers.push(tokens[1].to_owned()),
            "options" => {
                for option in &tokens[1..] {
                    match *option {
                        "rotate" => config.rotate = true,
                        "edns0" => config.edns = true,
                        _ => {}
                    }
                }
            }
            _ => {}
        }
    }
    if config.nameservers.is_empty() {
        return Err(ResolveError::Other("no nameservers".to_owned()));
    }
    // The `nameservers` setter checks every entry; DNS-over-HTTPS URLs are
    // not supported (PLAN.md D-16).
    for nameserver in &config.nameservers {
        if py_ip_address(nameserver).is_none() {
            if nameserver.to_ascii_lowercase().starts_with("https:") {
                return Err(ResolveError::Value(format!(
                    "DNS-over-HTTPS nameserver {nameserver} is not supported"
                )));
            }
            return Err(not_a_nameserver(nameserver));
        }
    }
    Ok(config)
}

/// A random 16-bit value (query IDs, rotation).
fn random_u64() -> u64 {
    std::collections::hash_map::RandomState::new()
        .build_hasher()
        .finish()
}

/// How one query attempt ended (`query_result(None, ex)`).
#[derive(Debug)]
enum AttemptError {
    /// `dns.exception.Timeout`: the nameserver is kept.
    Timeout,
    /// `FormError`, `EOFError`, `OSError`: the nameserver is dropped.
    Drop(String),
    /// `dns.message.Truncated` over UDP.
    Truncated,
    /// Any other exception: the nameserver is kept.
    Keep(String),
}

/// One nameserver of the resolution (`Do53Nameserver`).
#[derive(Clone, Debug)]
struct Nameserver {
    id: usize,
    text: String,
}

/// The socket address of a nameserver; a zone (`fe80::1%eth0`) is resolved
/// by `getaddrinfo` like dnspython's `low_level_address_tuple`.
async fn nameserver_addr(text: &str, port: u16) -> Result<SocketAddr, AttemptError> {
    let address =
        py_ip_address(text).ok_or_else(|| AttemptError::Drop(format!("invalid address {text}")))?;
    if address.scope.is_none() {
        return Ok(SocketAddr::new(address.addr, port));
    }
    tokio::net::lookup_host((text, port))
        .await
        .map_err(|err| AttemptError::Drop(err.to_string()))?
        .next()
        .ok_or_else(|| AttemptError::Drop(format!("cannot resolve {text}")))
}

/// What the query needs to send and to recognise its response.
struct Query {
    id: u16,
    wire: Vec<u8>,
    question: Question,
}

/// The source address of the query: `None` (any), an address, or a value
/// dnspython cannot bind (a non-string `source_ip` such as `5`, which
/// passes validation as an integer address and then fails inside every
/// attempt without dropping the nameserver).
enum Source {
    Any,
    Addr(IpAddr),
    Unusable(String),
}

fn source_of(value: &PyJson) -> Source {
    if !py_truthy(value) {
        return Source::Any;
    }
    match value {
        PyJson::Str(text) => match py_ip_address(text) {
            Some(address) => Source::Addr(address.addr),
            None => Source::Unusable(format!("invalid source {text}")),
        },
        other => Source::Unusable(format!("source {} is not a string", py_str(other))),
    }
}

fn bind_addr(source: &Source, dest: &SocketAddr) -> Result<SocketAddr, AttemptError> {
    match source {
        Source::Any => Ok(match dest {
            SocketAddr::V4(_) => SocketAddr::new(IpAddr::from([0u8; 4]), 0),
            SocketAddr::V6(_) => SocketAddr::new(IpAddr::from([0u8; 16]), 0),
        }),
        Source::Addr(addr) => {
            if addr.is_ipv4() != dest.is_ipv4() {
                // getaddrinfo of the source in the other family.
                return Err(AttemptError::Drop(
                    "[Errno -9] Address family for hostname not supported".to_owned(),
                ));
            }
            Ok(SocketAddr::new(*addr, 0))
        }
        Source::Unusable(message) => Err(AttemptError::Keep(message.clone())),
    }
}

fn os_error(err: &std::io::Error) -> AttemptError {
    AttemptError::Drop(pyos::os_error_str(err))
}

/// `dns.asyncquery.udp(..., raise_on_truncation=True, ignore_errors=True,
/// ignore_unexpected=True)`.
async fn udp_attempt(
    query: &Query,
    dest: SocketAddr,
    source: &Source,
    timeout: Duration,
) -> Result<Message, AttemptError> {
    let deadline = tokio::time::Instant::now() + timeout;
    let socket = UdpSocket::bind(bind_addr(source, &dest)?)
        .await
        .map_err(|err| os_error(&err))?;
    socket
        .send_to(&query.wire, dest)
        .await
        .map_err(|err| os_error(&err))?;
    let mut buf = vec![0u8; 65535];
    loop {
        let (n, from) = match tokio::time::timeout_at(deadline, socket.recv_from(&mut buf)).await {
            Err(_) => return Err(AttemptError::Timeout),
            Ok(Err(err)) => return Err(os_error(&err)),
            Ok(Ok(received)) => received,
        };
        if from != dest {
            continue;
        }
        match wire::parse_message(&buf[..n]) {
            Ok(message) => {
                if !wire::is_response(query.id, &query.question, &message) {
                    continue;
                }
                if message.flags & wire::FLAG_TC != 0 {
                    return Err(AttemptError::Truncated);
                }
                return Ok(message);
            }
            Err(Partial {
                message: Some(partial),
            }) if partial.flags & wire::FLAG_TC != 0 => {
                if wire::is_response(query.id, &query.question, &partial) {
                    return Err(AttemptError::Truncated);
                }
            }
            Err(_) => {}
        }
    }
}

/// `dns.asyncquery.tcp`: one deadline for connect, send and receive.
async fn tcp_attempt(
    query: &Query,
    dest: SocketAddr,
    source: &Source,
    timeout: Duration,
) -> Result<Message, AttemptError> {
    let deadline = tokio::time::Instant::now() + timeout;
    let bind = bind_addr(source, &dest)?;
    let socket = if dest.is_ipv4() {
        TcpSocket::new_v4()
    } else {
        TcpSocket::new_v6()
    }
    .map_err(|err| os_error(&err))?;
    if !matches!(source, Source::Any) {
        socket.bind(bind).map_err(|err| os_error(&err))?;
    }
    let exchange = async {
        let mut stream = socket.connect(dest).await.map_err(|err| os_error(&err))?;
        let mut framed = u16::try_from(query.wire.len())
            .expect("queries are small")
            .to_be_bytes()
            .to_vec();
        framed.extend_from_slice(&query.wire);
        stream
            .write_all(&framed)
            .await
            .map_err(|err| os_error(&err))?;
        let mut length = [0u8; 2];
        read_exactly(&mut stream, &mut length).await?;
        let mut body = vec![0u8; usize::from(u16::from_be_bytes(length))];
        read_exactly(&mut stream, &mut body).await?;
        Ok::<Vec<u8>, AttemptError>(body)
    };
    let body = tokio::time::timeout_at(deadline, exchange)
        .await
        .map_err(|_| AttemptError::Timeout)??;
    let message = wire::parse_message(&body).map_err(|_| {
        AttemptError::Drop("The DNS packet passed to from_wire() is invalid.".to_owned())
    })?;
    if !wire::is_response(query.id, &query.question, &message) {
        return Err(AttemptError::Drop(
            "A DNS query response does not respond to the question asked.".to_owned(),
        ));
    }
    Ok(message)
}

/// `_read_exactly`: EOF before the count is an `EOFError`.
async fn read_exactly(
    stream: &mut tokio::net::TcpStream,
    buf: &mut [u8],
) -> Result<(), AttemptError> {
    let mut filled = 0;
    while filled < buf.len() {
        let n = stream
            .read(&mut buf[filled..])
            .await
            .map_err(|err| os_error(&err))?;
        if n == 0 {
            return Err(AttemptError::Drop("EOF".to_owned()));
        }
        filled += n;
    }
    Ok(())
}

/// A successful resolution: the record texts.
#[derive(Debug)]
pub struct Answer {
    pub records: Vec<String>,
}

fn sleep_seconds(seconds: f64) -> Duration {
    Duration::from_secs_f64(seconds)
}

/// `Resolver.resolve(qname, query_type, source=...)` after `Resolver()` and
/// the nameserver override of `DnspythonResolver.query`.
async fn resolve(
    qname: &Name,
    config: &DnsConfig,
    system: &SystemConfig,
    nameservers: Vec<String>,
    settings: &DnsSettings,
) -> Result<Answer, ResolveError> {
    let lifetime = config.timeout;
    let source = source_of(&config.source_ip);
    let id = (random_u64() & 0xffff) as u16;
    let edns = system.edns.then_some(EDNS_PAYLOAD);
    let query = Query {
        id,
        wire: wire::build_query(id, qname, config.query_type, edns),
        question: Question {
            name: qname.clone(),
            rtype: config.query_type.code(),
            class: wire::CLASS_IN,
        },
    };
    let mut pool: Vec<Nameserver> = nameservers
        .into_iter()
        .enumerate()
        .map(|(id, text)| Nameserver { id, text })
        .collect();
    if system.rotate {
        // random.shuffle
        for i in (1..pool.len()).rev() {
            let j = usize::try_from(random_u64() % (i as u64 + 1)).unwrap_or(0);
            pool.swap(i, j);
        }
    }
    let mut current: VecDeque<Nameserver> = pool.iter().cloned().collect();
    let mut errors: Vec<String> = Vec::new();
    let mut backoff = 0.10;
    let mut retry_with_tcp = false;
    let mut nameserver: Option<Nameserver> = None;
    let start = Instant::now();
    loop {
        // next_nameserver()
        let tcp = if retry_with_tcp {
            retry_with_tcp = false;
            true
        } else {
            if current.is_empty() {
                if pool.is_empty() {
                    return Err(ResolveError::Other(format!(
                        "All nameservers failed to answer the query {} IN {}: {}",
                        qname.to_text(),
                        config.query_type.as_str(),
                        errors.join("; ")
                    )));
                }
                current = pool.iter().cloned().collect();
                tokio::time::sleep(sleep_seconds(backoff)).await;
                backoff = f64::min(backoff * 2.0, 2.0);
            }
            nameserver = current.pop_front();
            false
        };
        let ns = nameserver.clone().expect("a nameserver was chosen");
        // _compute_timeout()
        let elapsed = start.elapsed().as_secs_f64();
        if elapsed >= lifetime {
            return Err(ResolveError::Timeout);
        }
        let timeout = sleep_seconds(f64::min(lifetime - elapsed, config.timeout));
        let attempt = async {
            let dest = nameserver_addr(&ns.text, settings.port).await?;
            if tcp {
                tcp_attempt(&query, dest, &source, timeout).await
            } else {
                udp_attempt(&query, dest, &source, timeout).await
            }
        };
        let response = match attempt.await {
            Ok(response) => response,
            Err(err) => {
                let server = format!("Server Do53:{}@{}", ns.text, settings.port);
                match err {
                    AttemptError::Timeout => {
                        errors.push(format!("{server} answered The DNS operation timed out."));
                    }
                    AttemptError::Keep(message) => {
                        errors.push(format!("{server} answered {message}"))
                    }
                    AttemptError::Drop(message) => {
                        errors.push(format!("{server} answered {message}"));
                        pool.retain(|n| n.id != ns.id);
                    }
                    AttemptError::Truncated => {
                        errors.push(format!("{server} answered The DNS response was truncated."));
                        if tcp {
                            pool.retain(|n| n.id != ns.id);
                        } else {
                            retry_with_tcp = true;
                        }
                    }
                }
                continue;
            }
        };
        let server = format!("Server Do53:{}@{}", ns.text, settings.port);
        match response.rcode() {
            wire::RCODE_NOERROR => match wire::resolve_chaining(&response) {
                Ok(Some(rdatas)) => {
                    let records = wire::record_texts(&rdatas).map_err(ResolveError::Value)?;
                    return Ok(Answer { records });
                }
                Ok(None) => return Err(ResolveError::NoAnswer),
                Err(err) => {
                    errors.push(format!("{server} answered {err}"));
                    pool.retain(|n| n.id != ns.id);
                }
            },
            wire::RCODE_NXDOMAIN => match wire::resolve_chaining(&response) {
                Ok(_) => return Err(ResolveError::NxDomain),
                Err(err) => {
                    errors.push(format!("{server} answered {err}"));
                    pool.retain(|n| n.id != ns.id);
                }
            },
            wire::RCODE_YXDOMAIN => {
                return Err(ResolveError::Other(
                    "The DNS query name is too long after DNAME substitution.".to_owned(),
                ));
            }
            rcode => {
                errors.push(format!("{server} answered {}", rcode_text(rcode)));
                pool.retain(|n| n.id != ns.id);
            }
        }
    }
}

/// `dns.rcode.to_text`.
fn rcode_text(rcode: u16) -> String {
    match rcode {
        0 => "NOERROR".to_owned(),
        1 => "FORMERR".to_owned(),
        2 => "SERVFAIL".to_owned(),
        3 => "NXDOMAIN".to_owned(),
        4 => "NOTIMP".to_owned(),
        5 => "REFUSED".to_owned(),
        6 => "YXDOMAIN".to_owned(),
        7 => "YXRRSET".to_owned(),
        8 => "NXRRSET".to_owned(),
        9 => "NOTAUTH".to_owned(),
        10 => "NOTZONE".to_owned(),
        16 => "BADVERS".to_owned(),
        other => other.to_string(),
    }
}

/// `DnspythonResolver.query`: `Resolver()`, the `dns_server` override, then
/// `resolve`.
///
/// # Errors
/// See [`ResolveError`].
pub async fn query(
    domain: &str,
    config: &DnsConfig,
    settings: &DnsSettings,
) -> Result<Answer, ResolveError> {
    let system = read_resolv_conf(settings)?;
    let nameservers = if py_truthy(&config.dns_server) {
        match &config.dns_server {
            PyJson::Str(text) if py_ip_address(text).is_some() => vec![text.clone()],
            other => return Err(not_a_nameserver(&py_str(other))),
        }
    } else {
        system.nameservers.clone()
    };
    let qname = wire::name_from_text(domain).map_err(|err| match err {
        wire::NameTextError::NonAscii => ResolveError::Value(err.to_string()),
        _ => ResolveError::Other(err.to_string()),
    })?;
    resolve(&qname, config, &system, nameservers, settings).await
}

fn result(check: &Check, status: ResultStatus, data: PyDict) -> CheckResult {
    CheckResult::new(check.check_id, status, data)
}

fn error_result(check: &Check, error_type: &str, message: String) -> CheckResult {
    let mut data = PyDict::new();
    data.insert("error_type".to_owned(), PyJson::from(error_type));
    data.insert("error_msg".to_owned(), PyJson::from(message));
    result(check, ResultStatus::Error, data)
}

fn strings(values: &[String]) -> PyJson {
    PyJson::List(values.iter().map(|v| PyJson::from(v.as_str())).collect())
}

/// `DnsCheckExecutor.execute`.
pub async fn execute(check: &Check, settings: &DnsSettings) -> CheckResult {
    let start = Instant::now();
    let config = match DnsConfig::from_data(&check.data) {
        Ok(config) => config,
        Err(DnsConfigError::Value(message)) => {
            return error_result(check, "configuration_error", message);
        }
        Err(DnsConfigError::Exception { message, .. }) => {
            return error_result(check, "unexpected_error", message);
        }
    };
    let url = &check.url;
    let answer = match query(url, &config, settings).await {
        Ok(answer) => answer,
        Err(err) => {
            let (error_type, message) = match err {
                ResolveError::NxDomain => ("nxdomain", format!("Domain {url} does not exist")),
                ResolveError::Timeout => ("timeout", format!("DNS query timed out for {url}")),
                ResolveError::NoAnswer => ("no_answer", format!("No answer received for {url}")),
                ResolveError::Value(message) => ("configuration_error", message),
                ResolveError::Other(message) => ("unexpected_error", message),
            };
            return error_result(check, error_type, message);
        }
    };
    let query_time_ms = i64::try_from(start.elapsed().as_millis()).unwrap_or(i64::MAX);
    let qname = wire::name_from_text(url).expect("resolved names parse");
    let mut metadata: Vec<(&str, PyJson)> = vec![
        ("response_code", PyJson::from("NOERROR")),
        (
            "questions",
            PyJson::List(vec![PyJson::from(format!(
                "{} IN {}",
                qname.to_text(),
                config.query_type.as_str()
            ))]),
        ),
        ("rrset", strings(&answer.records)),
        (
            "dns_server",
            if py_truthy(&config.dns_server) {
                config.dns_server.clone()
            } else {
                PyJson::from("system")
            },
        ),
    ];
    if py_truthy(&config.source_ip) {
        metadata.push(("source_address", config.source_ip.clone()));
    }
    let matched = check_ip_match(
        &answer.records,
        &config.expected_ips,
        config.query_type.ip_version().is_some(),
    );
    let mut data = PyDict::new();
    let status = if matched {
        data.insert("resolved_ips".to_owned(), strings(&answer.records));
        ResultStatus::Ok
    } else {
        data.insert("error_type".to_owned(), PyJson::from("resolution_mismatch"));
        data.insert("expected".to_owned(), strings(&config.expected_ips));
        data.insert("actual".to_owned(), strings(&answer.records));
        ResultStatus::Error
    };
    data.insert("query_time_ms".to_owned(), PyJson::Int(query_time_ms));
    for (key, value) in metadata {
        data.insert(key.to_owned(), value);
    }
    result(check, status, data)
}

#[cfg(test)]
mod tests;
