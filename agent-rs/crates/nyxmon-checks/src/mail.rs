//! What the SMTP and IMAP executors take from their surroundings: clock,
//! randomness, retry sleep, the local host name and the log. Tests inject
//! all of it; [`SystemMailEnv`] is the real one.

use std::sync::OnceLock;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use nyxmon_core::log::{LogSink, TracingSink};

/// Environment of a mail check execution.
pub trait MailEnv: Send + Sync {
    /// `time.time()`.
    fn now(&self) -> f64;
    /// `secrets.token_hex(3)`.
    fn token(&self) -> String;
    /// `email.utils.make_msgid(domain=domain)`.
    fn message_id(&self, domain: &str) -> String;
    /// `anyio.sleep(delay)` between attempts.
    fn sleep(&self, delay: Duration) -> impl Future<Output = ()> + Send;
    /// smtplib's default EHLO name (`socket.getfqdn()` or `[address]`).
    fn local_hostname(&self) -> impl Future<Output = String> + Send;
    /// Receiver of the TLS verification warning.
    fn log(&self) -> &dyn LogSink;
}

/// The production environment.
#[derive(Debug, Default, Clone, Copy)]
pub struct SystemMailEnv;

impl MailEnv for SystemMailEnv {
    fn now(&self) -> f64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0.0, |d| d.as_secs_f64())
    }

    fn token(&self) -> String {
        let bytes: [u8; 3] = rand::random();
        bytes.iter().map(|b| format!("{b:02x}")).collect()
    }

    fn message_id(&self, domain: &str) -> String {
        let centis = (self.now() * 100.0) as u64;
        let random: u64 = rand::random();
        format!("<{centis}.{}.{random}@{domain}>", std::process::id())
    }

    async fn sleep(&self, delay: Duration) {
        tokio::time::sleep(delay).await;
    }

    async fn local_hostname(&self) -> String {
        static NAME: OnceLock<String> = OnceLock::new();
        if let Some(name) = NAME.get() {
            return name.clone();
        }
        let name = tokio::task::spawn_blocking(smtp_local_hostname)
            .await
            .unwrap_or_else(|_| "[127.0.0.1]".to_owned());
        NAME.get_or_init(|| name).clone()
    }

    fn log(&self) -> &dyn LogSink {
        &TracingSink
    }
}

/// `socket.getfqdn()`: the host name, replaced by the name its first
/// address resolves back to (`gethostbyaddr`; aliases are not available
/// through `getnameinfo`).
pub fn getfqdn() -> String {
    let Ok(name) = dns_lookup::get_hostname() else {
        return String::new();
    };
    let reverse = dns_lookup::lookup_host(&name)
        .ok()
        .and_then(|mut addrs| addrs.next())
        .and_then(|addr| dns_lookup::lookup_addr(&addr).ok());
    reverse.unwrap_or(name)
}

/// `smtplib.SMTP.__init__`'s `local_hostname`: the FQDN when it has a dot,
/// else `[<IPv4 of gethostname()>]` (`127.0.0.1` when that fails).
pub fn smtp_local_hostname() -> String {
    let fqdn = getfqdn();
    if fqdn.contains('.') {
        return fqdn;
    }
    let addr = dns_lookup::get_hostname()
        .ok()
        .and_then(|name| dns_lookup::lookup_host(&name).ok())
        .and_then(|mut addrs| addrs.find(std::net::IpAddr::is_ipv4))
        .map_or_else(|| "127.0.0.1".to_owned(), |addr| addr.to_string());
    format!("[{addr}]")
}
