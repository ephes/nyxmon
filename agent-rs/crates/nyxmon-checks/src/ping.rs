//! The ping check executor (`executors/ping_executor.py:PingCheckExecutor`):
//! resolve the target, then run the system `ping` binary once per attempt.
//! PLAN.md sections 1.6 (result shape) and 5.13.
//!
//! The process runner, the resolver and the sleep between attempts are
//! injectable like the Python constructor arguments; the pure parts live in
//! [`nyxmon_core::ping_text`].

use std::ffi::OsStr;
use std::future::Future;
use std::io;
use std::net::SocketAddr;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::process::ExitStatusExt;
use std::path::Path;
use std::pin::Pin;
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use nyxmon_core::config::ping::{PingConfig, TARGET_REQUIRED};
use nyxmon_core::config::{ConfigOutcome, PyException};
use nyxmon_core::model::{Check, CheckResult, ResultStatus};
use nyxmon_core::ping_text::{
    self, FALLBACK_PING_PATHS, FALLBACK_PING6_PATHS, PERMISSION_HINT, PROCESS_GRACE_SECONDS,
    build_ping_command, decode_output, error_detail, is_permission_failure, packet_stats, parse_ip,
    parse_rtt_ms, rtt_stats,
};
use nyxmon_core::pyjson::{PyDict, PyJson, py_format_g};

use crate::ctx::Sleeper;
use crate::happy_eyeballs::c_name;
use crate::idna2008::idna2008_resolve;
use crate::pyos::{os_error_str, os_error_with_filename};

/// A boxed, sendable future.
pub type BoxFuture<T> = Pin<Box<dyn Future<Output = T> + Send>>;

/// `ProcessOutput`: the captured output of one `ping` run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProcessOutput {
    /// The exit code, or `-signal` when a signal ended the process.
    pub returncode: i64,
    pub stdout: String,
    pub stderr: String,
}

/// Why a process run produced no [`ProcessOutput`]; each variant carries
/// `str(exc)` where Python formats it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RunError {
    /// `TimeoutError`: the timeout expired and the process was killed.
    Timeout,
    /// `FileNotFoundError` (`ENOENT`).
    NotFound(String),
    /// `PermissionError` (`EACCES`, `EPERM`).
    PermissionDenied(String),
    /// Any other `OSError`.
    Os(String),
    /// A non-`OSError` exception, which escapes the executor.
    Exception(PyException),
}

impl RunError {
    /// The `OSError` subclass Python raises when starting `program` fails
    /// with `err` (`[Errno N] <strerror>: '<program>'`).
    pub fn from_spawn(err: &io::Error, program: &str) -> RunError {
        let message = os_error_with_filename(err, program);
        match err.raw_os_error() {
            // ENOENT; EPERM and EACCES (the same numbers on Linux and macOS).
            Some(2) => RunError::NotFound(message),
            Some(1 | 13) => RunError::PermissionDenied(message),
            Some(_) => RunError::Os(message),
            None => RunError::Os(os_error_str(err)),
        }
    }
}

/// `RunProcess`: runs a command with a hard timeout in seconds.
pub type RunProcess =
    Arc<dyn Fn(Vec<String>, f64) -> BoxFuture<Result<ProcessOutput, RunError>> + Send + Sync>;

/// `Resolver`: a host name to IP address texts, or `str(exc)` of the
/// `socket.gaierror`/`OSError`/`UnicodeError` it raised.
pub type Resolver = Arc<dyn Fn(String) -> BoxFuture<Result<Vec<String>, String>> + Send + Sync>;

/// The exit code Python reports (`Popen.returncode`).
fn returncode(status: std::process::ExitStatus) -> i64 {
    match (status.code(), status.signal()) {
        (Some(code), _) => i64::from(code),
        (None, Some(signal)) => -i64::from(signal),
        (None, None) => -1,
    }
}

/// `run_process`: run `command` with the inherited environment plus
/// `LC_ALL=C` and `LANG=C`, stdin from `/dev/null`, capturing both output
/// streams; killed when `timeout` seconds pass.
pub async fn run_process(command: Vec<String>, timeout: f64) -> Result<ProcessOutput, RunError> {
    let Some(program) = command.first() else {
        // `subprocess` refuses an empty argument list.
        return Err(RunError::Exception(PyException::VALUE_ERROR));
    };
    if command.iter().any(|arg| arg.contains('\0')) {
        // `ValueError: embedded null byte`.
        return Err(RunError::Exception(PyException::VALUE_ERROR));
    }
    let limit = Duration::try_from_secs_f64(timeout).unwrap_or(Duration::MAX);
    let run = async {
        let child = tokio::process::Command::new(program)
            .args(&command[1..])
            .env("LC_ALL", "C")
            .env("LANG", "C")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .map_err(|err| RunError::from_spawn(&err, program))?;
        let output = child
            .wait_with_output()
            .await
            .map_err(|err| RunError::Os(os_error_str(&err)))?;
        Ok(ProcessOutput {
            returncode: returncode(output.status),
            stdout: decode_output(&output.stdout),
            stderr: decode_output(&output.stderr),
        })
    };
    // Dropping the child on timeout kills it (`kill_on_drop`).
    tokio::time::timeout(limit, run)
        .await
        .unwrap_or(Err(RunError::Timeout))
}

/// `inet_ntop` text of an IPv6 address: like Rust's, except that an
/// IPv4-compatible address (`::a.b.c.d`) keeps the dotted form.
fn ipv6_text(ip: std::net::Ipv6Addr) -> String {
    let segments = ip.segments();
    if segments[..6].iter().all(|&s| s == 0) && segments[6] != 0 {
        let [.., a, b, c, d] = ip.octets();
        return format!("::{a}.{b}.{c}.{d}");
    }
    ip.to_string()
}

/// The address text of anyio's `getaddrinfo` (`convert_ipv6_sockaddr`
/// appends a non-zero scope id).
fn address_text(addr: SocketAddr) -> String {
    match addr {
        SocketAddr::V4(v4) => v4.ip().to_string(),
        SocketAddr::V6(v6) if v6.scope_id() != 0 => {
            format!("{}%{}", ipv6_text(*v6.ip()), v6.scope_id())
        }
        SocketAddr::V6(v6) => ipv6_text(*v6.ip()),
    }
}

/// `resolve_host`: `anyio.getaddrinfo(host, None, type=SOCK_DGRAM)`
/// addresses in resolver order without duplicates. A non-ASCII name is
/// IDNA 2008 encoded first, as anyio does.
pub async fn resolve_host(host: String) -> Result<Vec<String>, String> {
    let name = idna2008_resolve(&host).map_err(|err| err.to_string())?;
    let resolved = tokio::net::lookup_host((c_name(&name), 0))
        .await
        .map_err(|err| os_error_str(&err))?;
    let mut addresses: Vec<String> = Vec::new();
    for addr in resolved {
        let text = address_text(addr);
        if !addresses.contains(&text) {
            addresses.push(text);
        }
    }
    Ok(addresses)
}

/// `os.path.join(directory, name)` for a relative `name`.
fn join(directory: &[u8], name: &str) -> Vec<u8> {
    let mut path = directory.to_vec();
    if !path.is_empty() && !path.ends_with(b"/") {
        path.push(b'/');
    }
    path.extend_from_slice(name.as_bytes());
    path
}

fn is_executable(path: &Path) -> bool {
    rustix::fs::access(
        path,
        rustix::fs::Access::EXISTS | rustix::fs::Access::EXEC_OK,
    )
    .is_ok()
}

/// `os.confstr("CS_PATH")`, which `shutil.which` uses without `PATH`.
const CS_PATH: &str = if cfg!(target_os = "macos") {
    "/usr/bin:/bin:/usr/sbin:/sbin"
} else {
    "/bin:/usr/bin"
};

/// `shutil.which(name)` for a bare command name with the given `PATH`
/// value: the first `PATH` entry holding an executable non-directory.
/// Entries that are not UTF-8 are skipped (PLAN.md D-17).
pub fn which(name: &str, path: Option<&OsStr>) -> Option<String> {
    let path = path.map_or(CS_PATH.as_bytes(), OsStrExt::as_bytes);
    if path.is_empty() {
        return None;
    }
    let mut seen: Vec<&[u8]> = Vec::new();
    for directory in path.split(|&byte| byte == b':') {
        if seen.contains(&directory) {
            continue;
        }
        seen.push(directory);
        let Ok(candidate) = String::from_utf8(join(directory, name)) else {
            continue;
        };
        let candidate_path = Path::new(&candidate);
        let is_file = candidate_path.metadata().is_ok_and(|meta| !meta.is_dir());
        if is_file && is_executable(candidate_path) {
            return Some(candidate);
        }
    }
    None
}

/// `find_binary(name, fallbacks)` with the given `PATH` value.
pub fn find_binary_in(name: &str, fallbacks: &[&str], path: Option<&OsStr>) -> Option<String> {
    which(name, path).or_else(|| {
        fallbacks
            .iter()
            .find(|candidate| {
                let candidate = Path::new(candidate);
                candidate.metadata().is_ok_and(|meta| meta.is_file()) && is_executable(candidate)
            })
            .map(|candidate| (*candidate).to_owned())
    })
}

/// `find_binary(name, fallbacks)`: `PATH` first, then the fallbacks.
pub fn find_binary(name: &str, fallbacks: &[&str]) -> Option<String> {
    find_binary_in(name, fallbacks, std::env::var_os("PATH").as_deref())
}

/// `{timeout:g}`.
fn seconds_text(seconds: f64) -> String {
    py_format_g(seconds)
}

/// The failure of one attempt: `(error_type, message)`.
type Failure = (&'static str, String);

fn error_record(attempt: i64, status: &str, message: &str) -> PyDict {
    let mut record = PyDict::new();
    record.insert("attempt".to_owned(), PyJson::Int(attempt));
    record.insert("status".to_owned(), PyJson::Str(status.to_owned()));
    record.insert("error_msg".to_owned(), PyJson::Str(message.to_owned()));
    record
}

/// `PingCheckExecutor._error`.
fn error_result(check: &Check, error_type: &str, message: String, extra: PyDict) -> CheckResult {
    let mut data = PyDict::new();
    data.insert("error_type".to_owned(), PyJson::Str(error_type.to_owned()));
    data.insert("error_msg".to_owned(), PyJson::Str(message));
    data.extend(extra);
    CheckResult::new(check.check_id, ResultStatus::Error, data)
}

/// The executor. Cheap to clone.
#[derive(Clone)]
pub struct PingExecutor {
    run: RunProcess,
    resolve: Resolver,
    sleeper: Sleeper,
    platform: String,
    ping_binary: Option<String>,
    ping6_binary: Option<String>,
}

impl Default for PingExecutor {
    fn default() -> Self {
        PingExecutor {
            run: Arc::new(|command, timeout| Box::pin(run_process(command, timeout))),
            resolve: Arc::new(|host| Box::pin(resolve_host(host))),
            sleeper: Sleeper::real(),
            platform: ping_text::sys_platform().to_owned(),
            ping_binary: None,
            ping6_binary: None,
        }
    }
}

impl PingExecutor {
    /// Replace the process runner.
    pub fn with_runner(mut self, run: RunProcess) -> Self {
        self.run = run;
        self
    }

    /// Replace the resolver.
    pub fn with_resolver(mut self, resolve: Resolver) -> Self {
        self.resolve = resolve;
        self
    }

    /// Replace the sleep between attempts.
    pub fn with_sleeper(mut self, sleeper: Sleeper) -> Self {
        self.sleeper = sleeper;
        self
    }

    /// Build command lines for another `sys.platform`.
    pub fn with_platform(mut self, platform: &str) -> Self {
        self.platform = platform.to_owned();
        self
    }

    /// Use these binaries instead of looking them up for each check.
    pub fn with_binaries(mut self, ping: Option<&str>, ping6: Option<&str>) -> Self {
        self.ping_binary = ping.map(str::to_owned);
        self.ping6_binary = ping6.map(str::to_owned);
        self
    }

    /// `PingCheckExecutor.execute`.
    ///
    /// # Errors
    /// The Python exception that would escape `execute` (the runner records
    /// it as `executor_exception`): a `ValueError` for a resolved address
    /// that is not an IP literal or a command line with a NUL character.
    pub async fn execute(&self, check: &Check) -> Result<CheckResult, PyException> {
        let config = match PingConfig::from_data(&check.data) {
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
        let Some(host) = config.target_host(&check.url) else {
            return Ok(error_result(
                check,
                "configuration_error",
                TARGET_REQUIRED.to_owned(),
                PyDict::new(),
            ));
        };
        let mut base = config.base_data(&host);

        let address = match self.resolve_target(&host, config.timeout).await {
            Ok(address) => address,
            Err((error_type, message)) => {
                return Ok(error_result(check, error_type, message, base));
            }
        };
        base.insert("target".to_owned(), PyJson::Str(address.clone()));
        if address != host {
            base.insert("hostname".to_owned(), PyJson::Str(host.clone()));
        }

        let ping_binary = match self.ping_binary.as_deref().filter(|path| !path.is_empty()) {
            Some(path) => path.to_owned(),
            None => match find_binary("ping", &FALLBACK_PING_PATHS) {
                Some(path) => path,
                None => {
                    return Ok(error_result(
                        check,
                        "ping_unavailable",
                        "No 'ping' binary found on PATH or in /sbin, /bin, /usr/bin, /usr/sbin"
                            .to_owned(),
                        base,
                    ));
                }
            },
        };
        let ping6_binary = match self.ping6_binary.as_deref().filter(|path| !path.is_empty()) {
            Some(path) => Some(path.to_owned()),
            None => find_binary("ping6", &FALLBACK_PING6_PATHS),
        };

        let command = build_ping_command(
            &self.platform,
            &address,
            config.timeout,
            &ping_binary,
            ping6_binary.as_deref(),
        )?;
        self.run_attempts(check, command, &config, base, &address)
            .await
    }

    /// `_run_attempts`: `config.count` single-echo attempts, summarised.
    async fn run_attempts(
        &self,
        check: &Check,
        command: Vec<String>,
        config: &PingConfig,
        base: PyDict,
        address: &str,
    ) -> Result<CheckResult, PyException> {
        let mut attempts: Vec<PyJson> = Vec::new();
        let mut all_timeouts = true;
        let mut rtts: Vec<f64> = Vec::new();
        let mut last_error: Option<Failure> = None;

        for attempt in 1..=config.count {
            if attempt > 1 && config.interval > 0.0 {
                self.sleeper.sleep(config.interval).await;
            }
            let (record, outcome) = self
                .attempt(attempt, command.clone(), config.timeout, address)
                .await?;
            all_timeouts &= record.get("status").and_then(PyJson::as_str) == Some("timeout");
            attempts.push(PyJson::Dict(record));
            let failure = match outcome {
                Ok(rtt) => {
                    rtts.push(rtt);
                    continue;
                }
                Err(failure) => failure,
            };
            if matches!(failure.0, "permission_error" | "ping_unavailable") {
                // Retrying cannot help.
                let mut data = base;
                data.extend(packet_stats(attempts.len() as i64, 0));
                data.insert("attempts".to_owned(), PyJson::List(attempts));
                return Ok(error_result(check, failure.0, failure.1, data));
            }
            last_error = Some(failure);
        }

        let sent = attempts.len() as i64;
        let mut data = base;
        data.extend(packet_stats(sent, rtts.len() as i64));
        data.insert("attempts".to_owned(), PyJson::List(attempts));
        if !rtts.is_empty() {
            let (min, max, avg) = rtt_stats(&rtts);
            data.insert("rtt_min_ms".to_owned(), PyJson::Float(min));
            data.insert("rtt_max_ms".to_owned(), PyJson::Float(max));
            data.insert("rtt_avg_ms".to_owned(), PyJson::Float(avg));
            data.insert(
                "rtt_list_ms".to_owned(),
                PyJson::List(rtts.into_iter().map(PyJson::Float).collect()),
            );
            return Ok(CheckResult::new(check.check_id, ResultStatus::Ok, data));
        }

        // At least one attempt ran and none succeeded.
        let (mut error_type, mut message) = last_error.unwrap_or(("timeout", String::new()));
        if all_timeouts {
            error_type = "timeout";
            message = format!(
                "No reply from {address} within {}s ({sent} attempt(s))",
                seconds_text(config.timeout)
            );
        }
        Ok(error_result(check, error_type, message, data))
    }

    /// `_attempt`: one echo request; its record and either the RTT or the
    /// failure.
    async fn attempt(
        &self,
        attempt: i64,
        command: Vec<String>,
        timeout: f64,
        address: &str,
    ) -> Result<(PyDict, Result<f64, Failure>), PyException> {
        let failed = |status: &str, error_type: &'static str, message: String| {
            let record = error_record(attempt, status, &message);
            Ok((record, Err((error_type, message))))
        };
        let output = match (self.run)(command, timeout + PROCESS_GRACE_SECONDS).await {
            Ok(output) => output,
            Err(RunError::Timeout) => {
                let message = format!("No reply within {}s", seconds_text(timeout));
                return failed("timeout", "timeout", message);
            }
            Err(RunError::NotFound(exc)) => {
                let message = format!("ping binary not found: {exc}");
                return failed("error", "ping_unavailable", message);
            }
            Err(RunError::PermissionDenied(exc)) => {
                let message = format!("{PERMISSION_HINT} ({exc})");
                return failed("error", "permission_error", message);
            }
            Err(RunError::Os(exc)) => {
                let message = format!("Could not run ping: {exc}");
                return failed("error", "execution_error", message);
            }
            Err(RunError::Exception(exc)) => return Err(exc),
        };

        if output.returncode == 0
            && let Some(rtt) = parse_rtt_ms(&output.stdout, Some(address))
        {
            let mut record = PyDict::new();
            record.insert("attempt".to_owned(), PyJson::Int(attempt));
            record.insert("status".to_owned(), PyJson::Str("ok".to_owned()));
            record.insert("rtt_ms".to_owned(), PyJson::Float(rtt));
            return Ok((record, Ok(rtt)));
        }

        let detail = error_detail(&output.stdout, &output.stderr);
        let combined = format!("{}\n{}", output.stdout, output.stderr);
        if is_permission_failure(&combined) {
            let message = if detail.is_empty() {
                PERMISSION_HINT.to_owned()
            } else {
                format!("{PERMISSION_HINT} ({detail})")
            };
            return failed("error", "permission_error", message);
        }

        let (status, message) = if detail.is_empty() {
            (
                "timeout",
                format!("No reply within {}s", seconds_text(timeout)),
            )
        } else {
            ("unreachable", detail)
        };
        let mut record = error_record(attempt, status, &message);
        record.insert("returncode".to_owned(), PyJson::Int(output.returncode));
        Ok((record, Err((status, message))))
    }

    /// `_resolve_target`: a literal address (an IPv6 zone or anything after
    /// `%` ignored for the check) passes through; a name resolves to its
    /// first address within `timeout` seconds.
    async fn resolve_target(
        &self,
        host: &str,
        timeout: f64,
    ) -> Result<String, (&'static str, String)> {
        if parse_ip(host).is_some() {
            return Ok(host.to_owned());
        }
        let limit = Duration::try_from_secs_f64(timeout).unwrap_or(Duration::MAX);
        match tokio::time::timeout(limit, (self.resolve)(host.to_owned())).await {
            Err(_) => Err((
                "dns_timeout",
                format!(
                    "Resolving {host} timed out after {}s",
                    seconds_text(timeout)
                ),
            )),
            Ok(Err(exc)) => Err(("dns_error", format!("Could not resolve {host}: {exc}"))),
            Ok(Ok(addresses)) => addresses.into_iter().next().ok_or_else(|| {
                (
                    "dns_error",
                    format!("Could not resolve {host}: no addresses"),
                )
            }),
        }
    }
}

#[cfg(test)]
mod tests;
