//! `anyio.connect_tcp`: resolution and the stateless Happy Eyeballs race of
//! anyio 4 (RFC 6555), used by the TCP check.
//!
//! - An IP address connects directly; a name is resolved with
//!   `getaddrinfo(name, port, type=SOCK_STREAM)` (a failure is the resolver
//!   error, Python's `socket.gaierror`).
//! - The addresses are ordered: the first IPv6 address, then the first IPv4
//!   address found after it, then the rest in resolver order.
//! - Attempts start one after another; the next one starts after 250 ms or
//!   as soon as the previous attempt failed. The first connection wins and
//!   the others are dropped.
//! - When every attempt failed the error is always
//!   `OSError("All connection attempts failed")`, also for a single address.

use std::io;
use std::net::SocketAddr;
use std::time::Duration;

use nyxmon_core::config::tcp::ConnectTarget;
use tokio::net::TcpStream;
use tokio::task::JoinSet;

/// `happy_eyeballs_delay`.
pub const HAPPY_EYEBALLS_DELAY: Duration = Duration::from_millis(250);

/// The message of a connection without any successful attempt.
pub const ALL_ATTEMPTS_FAILED: &str = "All connection attempts failed";

/// Why `connect_tcp` failed.
#[derive(Debug)]
pub enum ConnectError {
    /// `getaddrinfo` failed.
    Resolve(io::Error),
    /// Every connection attempt failed.
    AllFailed,
}

/// anyio's address order: the first IPv6 address first, the first IPv4
/// address after an IPv6 one second, everything else in resolver order.
pub fn order_addresses(resolved: &[SocketAddr]) -> Vec<SocketAddr> {
    let mut ordered: Vec<SocketAddr> = Vec::with_capacity(resolved.len());
    let (mut v6_found, mut v4_found) = (false, false);
    for addr in resolved {
        if addr.is_ipv6() && !v6_found {
            v6_found = true;
            ordered.insert(0, *addr);
        } else if addr.is_ipv4() && !v4_found && v6_found {
            v4_found = true;
            ordered.insert(1.min(ordered.len()), *addr);
        } else {
            ordered.push(*addr);
        }
    }
    ordered
}

/// The name `getaddrinfo` sees: C strings end at the first NUL byte.
fn c_name(name: &str) -> &str {
    name.split('\0').next().unwrap_or("")
}

/// Resolve `target` to the ordered connection addresses. A
/// [`ConnectTarget::Name`] must already be IDNA-encoded
/// ([`crate::idna2008::idna2008_resolve`]).
///
/// # Errors
/// The resolver error for a name that does not resolve.
pub async fn resolve(target: &ConnectTarget, port: u16) -> Result<Vec<SocketAddr>, io::Error> {
    match target {
        ConnectTarget::Address(addr) => Ok(vec![SocketAddr::new(*addr, port)]),
        // anyio connects to the literal; the resolver failing on its zone
        // is a failed attempt, not a resolution error.
        ConnectTarget::ScopedAddress(literal) => {
            Ok(tokio::net::lookup_host((c_name(literal), port))
                .await
                .map(Iterator::collect)
                .unwrap_or_default())
        }
        ConnectTarget::Name(name) => {
            let resolved: Vec<SocketAddr> = tokio::net::lookup_host((c_name(name), port))
                .await?
                .collect();
            Ok(order_addresses(&resolved))
        }
    }
}

/// Race connection attempts to `addrs` (already ordered).
///
/// # Errors
/// [`ConnectError::AllFailed`] when no attempt connected.
pub async fn connect_addresses(addrs: Vec<SocketAddr>) -> Result<TcpStream, ConnectError> {
    let mut attempts = JoinSet::new();
    for (index, addr) in addrs.into_iter().enumerate() {
        attempts.spawn(async move { (index, TcpStream::connect(addr).await) });
        let delay = tokio::time::sleep(HAPPY_EYEBALLS_DELAY);
        tokio::pin!(delay);
        loop {
            tokio::select! {
                () = &mut delay => break,
                Some(joined) = attempts.join_next() => match joined {
                    Ok((_, Ok(stream))) => return Ok(stream),
                    // The current attempt failed: start the next one now.
                    Ok((finished, Err(_))) if finished == index => break,
                    _ => {}
                },
                else => break,
            }
        }
    }
    while let Some(joined) = attempts.join_next().await {
        if let Ok((_, Ok(stream))) = joined {
            return Ok(stream);
        }
    }
    Err(ConnectError::AllFailed)
}

/// `anyio.connect_tcp(target, port)` without TLS.
///
/// # Errors
/// See [`ConnectError`].
pub async fn connect_tcp(target: &ConnectTarget, port: u16) -> Result<TcpStream, ConnectError> {
    let addrs = resolve(target, port).await.map_err(ConnectError::Resolve)?;
    connect_addresses(addrs).await
}

#[cfg(test)]
mod tests {
    use super::*;

    fn addr(text: &str) -> SocketAddr {
        text.parse().unwrap()
    }

    #[test]
    fn ipv6_first_then_the_first_ipv4_after_it() {
        let resolved = [
            addr("192.0.2.1:1"),
            addr("192.0.2.2:1"),
            addr("[2001:db8::1]:1"),
            addr("192.0.2.3:1"),
            addr("[2001:db8::2]:1"),
            addr("192.0.2.4:1"),
        ];
        assert_eq!(
            order_addresses(&resolved),
            vec![
                addr("[2001:db8::1]:1"),
                addr("192.0.2.3:1"),
                addr("192.0.2.1:1"),
                addr("192.0.2.2:1"),
                addr("[2001:db8::2]:1"),
                addr("192.0.2.4:1"),
            ]
        );
        let only_v4 = [addr("192.0.2.1:1"), addr("192.0.2.2:1")];
        assert_eq!(order_addresses(&only_v4), only_v4.to_vec());
        let v6_first = [addr("[2001:db8::1]:1"), addr("192.0.2.1:1")];
        assert_eq!(order_addresses(&v6_first), v6_first.to_vec());
    }

    #[test]
    fn names_end_at_nul() {
        assert_eq!(c_name("localhost\0junk"), "localhost");
        assert_eq!(c_name("plain"), "plain");
    }

    #[tokio::test]
    async fn a_refused_attempt_starts_the_next_one_immediately() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let open = listener.local_addr().unwrap();
        let closed = {
            let probe = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            probe.local_addr().unwrap()
        };
        let started = tokio::time::Instant::now();
        let stream = connect_addresses(vec![closed, open]).await.unwrap();
        assert_eq!(stream.peer_addr().unwrap(), open);
        assert!(started.elapsed() < HAPPY_EYEBALLS_DELAY);
    }

    #[tokio::test]
    async fn every_attempt_failing_is_all_failed() {
        let closed = {
            let probe = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            probe.local_addr().unwrap()
        };
        assert!(matches!(
            connect_addresses(vec![closed]).await,
            Err(ConnectError::AllFailed)
        ));
        assert!(matches!(
            connect_addresses(vec![closed, closed]).await,
            Err(ConnectError::AllFailed)
        ));
    }

    #[tokio::test]
    async fn localhost_resolves_through_the_system_resolver() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let target = ConnectTarget::Name("localhost".to_owned());
        let stream = connect_tcp(&target, port).await.unwrap();
        assert!(stream.peer_addr().unwrap().ip().is_loopback());
    }
}
