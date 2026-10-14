//! The ping executor: the scripted Python runs of `fixtures/ping_text.json`
//! replayed with stub runner, resolver and sleep, and the real process
//! runner against fake `ping` scripts.

use std::ffi::OsString;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use nyxmon_core::pyjson::{loads, py_str_repr};

use super::*;

fn fixture() -> PyJson {
    loads(include_str!("../../../../fixtures/ping_text.json")).expect("valid fixture")
}

fn field<'a>(value: &'a PyJson, key: &str) -> &'a PyJson {
    value
        .get(key)
        .unwrap_or_else(|| panic!("fixture field {key:?} missing"))
}

fn text<'a>(value: &'a PyJson, key: &str) -> &'a str {
    field(value, key).as_str().expect("a string")
}

fn list<'a>(value: &'a PyJson, key: &str) -> &'a [PyJson] {
    field(value, key).as_list().expect("a list")
}

fn number(value: &PyJson) -> f64 {
    match value {
        PyJson::Float(x) => *x,
        PyJson::Int(n) => *n as f64,
        other => panic!("not a number: {other:?}"),
    }
}

fn check(url: &str, data: &str) -> Check {
    Check {
        check_id: 7,
        service_id: 1,
        name: "ping".to_owned(),
        check_type: "ping".to_owned(),
        url: url.to_owned(),
        check_interval: 60,
        next_check_time: 0,
        processing_started_at: 0,
        status: "processing".to_owned(),
        disabled: false,
        data: loads(data).expect("valid data"),
        claim_started_at: 0,
    }
}

fn data_json(result: &CheckResult) -> String {
    PyJson::Dict(result.data.clone())
        .to_json_string()
        .expect("serialisable")
}

type Calls = Arc<Mutex<Vec<(Vec<String>, f64)>>>;

/// `_PingStubRunner`: replays scripted outcomes, recording the calls.
fn stub_runner(outcomes: &[PyJson]) -> (RunProcess, Calls) {
    let outcomes: Arc<Mutex<Vec<PyJson>>> = Arc::new(Mutex::new(outcomes.to_vec()));
    let calls: Calls = Arc::default();
    let recorded = calls.clone();
    let runner: RunProcess = Arc::new(move |command: Vec<String>, timeout: f64| {
        recorded.lock().unwrap().push((command.clone(), timeout));
        let outcome = outcomes.lock().unwrap().remove(0);
        let spawn_error =
            |errno: i32| RunError::from_spawn(&io::Error::from_raw_os_error(errno), &command[0]);
        let result = match outcome.get("raise").and_then(PyJson::as_str) {
            Some("timeout") => Err(RunError::Timeout),
            Some("not_found") => Err(spawn_error(2)),
            Some("permission") => Err(spawn_error(13)),
            Some("exec_format") => Err(spawn_error(8)),
            Some(other) => panic!("unknown outcome {other}"),
            None => Ok(ProcessOutput {
                returncode: field(&outcome, "returncode").as_i64().expect("int"),
                stdout: text(&outcome, "stdout").to_owned(),
                stderr: text(&outcome, "stderr").to_owned(),
            }),
        };
        Box::pin(async move { result })
    });
    (runner, calls)
}

/// The scenario resolver: addresses, an error message or a hang.
fn stub_resolver(spec: &PyJson) -> (Resolver, Arc<Mutex<Vec<String>>>) {
    let calls: Arc<Mutex<Vec<String>>> = Arc::default();
    let recorded = calls.clone();
    let spec = spec.clone();
    let resolver: Resolver = Arc::new(move |host: String| {
        recorded.lock().unwrap().push(host);
        let outcome = match &spec {
            PyJson::Str(hang) if hang == "hang" => None,
            PyJson::Dict(error) => Some(Err(error["error"].as_str().unwrap().to_owned())),
            PyJson::List(addresses) => Some(Ok(addresses
                .iter()
                .map(|a| a.as_str().unwrap().to_owned())
                .collect())),
            other => panic!("unknown resolver {other:?}"),
        };
        Box::pin(async move {
            match outcome {
                Some(result) => result,
                None => std::future::pending().await,
            }
        })
    });
    (resolver, calls)
}

#[tokio::test]
async fn scripted_runs_match_python() {
    let fixture = fixture();
    let scenarios = list(&fixture, "scenarios");
    assert!(scenarios.len() > 40);
    for scenario in scenarios {
        let name = text(scenario, "name");
        let (runner, runner_calls) = stub_runner(list(scenario, "outcomes"));
        let (resolver, resolver_calls) = stub_resolver(field(scenario, "resolver"));
        let (sleeper, sleeps) = Sleeper::recording();
        let executor = PingExecutor::default()
            .with_runner(runner)
            .with_resolver(resolver)
            .with_sleeper(sleeper)
            .with_platform(text(scenario, "platform"))
            .with_binaries(Some("/bin/ping"), Some("/sbin/ping6"));
        let outcome = executor
            .execute(&check(text(scenario, "url"), text(scenario, "data_json")))
            .await;
        match scenario.get("exception") {
            Some(class) => {
                assert_eq!(
                    outcome.map(|_| ()),
                    Err(PyException(match class.as_str().unwrap() {
                        "ValueError" => "ValueError",
                        other => panic!("{name}: unexpected class {other}"),
                    })),
                    "{name}"
                );
            }
            None => {
                let result = outcome.unwrap_or_else(|exc| panic!("{name}: {exc:?}"));
                assert_eq!(result.status.as_str(), text(scenario, "status"), "{name}");
                assert_eq!(data_json(&result), text(scenario, "result_json"), "{name}");
            }
        }
        let expected_calls: Vec<(Vec<String>, u64)> = list(scenario, "runner_calls")
            .iter()
            .map(|call| {
                let call = call.as_list().unwrap();
                let command = call[0]
                    .as_list()
                    .unwrap()
                    .iter()
                    .map(|arg| arg.as_str().unwrap().to_owned())
                    .collect();
                (command, number(&call[1]).to_bits())
            })
            .collect();
        let actual_calls: Vec<(Vec<String>, u64)> = runner_calls
            .lock()
            .unwrap()
            .iter()
            .map(|(command, timeout)| (command.clone(), timeout.to_bits()))
            .collect();
        assert_eq!(actual_calls, expected_calls, "{name}");
        let expected_hosts: Vec<String> = list(scenario, "resolver_calls")
            .iter()
            .map(|host| host.as_str().unwrap().to_owned())
            .collect();
        assert_eq!(*resolver_calls.lock().unwrap(), expected_hosts, "{name}");
        let expected_sleeps: Vec<u64> = list(scenario, "sleeps")
            .iter()
            .map(|s| number(s).to_bits())
            .collect();
        let actual_sleeps: Vec<u64> = sleeps.lock().unwrap().iter().map(|s| s.to_bits()).collect();
        assert_eq!(actual_sleeps, expected_sleeps, "{name}");
    }
}

// ------------------------------------------------------------ real processes

/// A temporary directory with executable scripts.
struct Scripts {
    dir: tempfile::TempDir,
}

impl Scripts {
    fn new() -> Self {
        Scripts {
            dir: tempfile::tempdir().expect("temp dir"),
        }
    }

    fn path(&self, name: &str) -> PathBuf {
        self.dir.path().join(name)
    }

    fn write(&self, name: &str, body: &str, mode: u32) -> String {
        let path = self.path(name);
        std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).expect("write script");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode)).expect("chmod");
        path.to_str().expect("UTF-8 temp path").to_owned()
    }
}

fn text_file_busy(message: &str) -> bool {
    // Another test thread forking while a script was being written can
    // briefly hold it open for writing (ETXTBSY); such a run is repeated.
    message.contains("Text file busy")
}

async fn run(command: &[&str], timeout: f64) -> Result<ProcessOutput, RunError> {
    for _ in 0..20 {
        let outcome = run_process(command.iter().map(|s| (*s).to_owned()).collect(), timeout).await;
        match &outcome {
            Err(RunError::Os(message)) if text_file_busy(message) => {
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
            _ => return outcome,
        }
    }
    panic!("script stayed busy");
}

async fn execute(executor: &PingExecutor, check: &Check) -> CheckResult {
    for _ in 0..20 {
        let result = executor.execute(check).await.expect("no exception");
        if !data_json(&result).contains("Text file busy") {
            return result;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    panic!("script stayed busy");
}

#[tokio::test]
async fn the_process_runner_captures_output_and_exit_code() {
    let output = run(&["sh", "-c", "echo out; echo err >&2; exit 3"], 5.0).await;
    assert_eq!(
        output,
        Ok(ProcessOutput {
            returncode: 3,
            stdout: "out\n".to_owned(),
            stderr: "err\n".to_owned(),
        })
    );
}

#[tokio::test]
async fn the_process_runner_sets_the_c_locale_and_closes_stdin() {
    let output = run(&["sh", "-c", "echo \"$LC_ALL $LANG\"; cat; echo done"], 5.0)
        .await
        .expect("runs");
    assert_eq!(output.stdout, "C C\ndone\n");
}

#[tokio::test]
async fn the_process_runner_kills_after_the_timeout() {
    let start = tokio::time::Instant::now();
    assert_eq!(run(&["sleep", "5"], 0.1).await, Err(RunError::Timeout));
    assert!(start.elapsed() < Duration::from_secs(3));
}

#[tokio::test]
async fn the_process_runner_reports_signals_and_replaces_bad_bytes() {
    let output = run(&["sh", "-c", "printf 'a\\377b'; kill -9 $$"], 5.0)
        .await
        .expect("runs");
    assert_eq!(output.returncode, -9);
    assert_eq!(output.stdout, "a\u{fffd}b");
}

#[tokio::test]
async fn start_failures_map_to_the_python_os_errors() {
    let scripts = Scripts::new();
    let missing = scripts.path("missing").to_str().unwrap().to_owned();
    assert_eq!(
        run(&[&missing], 5.0).await,
        Err(RunError::NotFound(format!(
            "[Errno 2] No such file or directory: {}",
            py_str_repr(&missing)
        )))
    );
    let plain = scripts.write("plain", "exit 0", 0o644);
    let dir = scripts.dir.path().to_str().unwrap().to_owned();
    for program in [&plain, &dir] {
        assert_eq!(
            run(&[program.as_str()], 5.0).await,
            Err(RunError::PermissionDenied(format!(
                "[Errno 13] Permission denied: {}",
                py_str_repr(program)
            )))
        );
    }
    assert_eq!(
        run(&["sh", "-c", "a\0b"], 5.0).await,
        Err(RunError::Exception(PyException::VALUE_ERROR))
    );
}

/// A fake iputils `ping` that logs its arguments.
fn fake_ping(scripts: &Scripts, body: &str) -> String {
    let log = scripts.path("args.log");
    scripts.write(
        "ping",
        &format!("echo \"$@\" >> '{}'\n{body}", log.display()),
        0o755,
    )
}

fn logged_args(scripts: &Scripts) -> String {
    std::fs::read_to_string(scripts.path("args.log")).unwrap_or_default()
}

fn linux_executor(ping: &str) -> PingExecutor {
    PingExecutor::default()
        .with_platform("linux")
        .with_binaries(Some(ping), None)
}

#[tokio::test]
async fn a_fake_iputils_ping_reports_round_trip_times() {
    let scripts = Scripts::new();
    let ping = fake_ping(
        &scripts,
        "echo 'PING 127.0.0.1 (127.0.0.1) 56(84) bytes of data.'\n\
         echo '64 bytes from 127.0.0.1: icmp_seq=1 ttl=64 time=0.046 ms'",
    );
    let result = execute(
        &linux_executor(&ping),
        &check(
            "127.0.0.1",
            r#"{"count": 2, "interval": 0, "timeout": 1.5}"#,
        ),
    )
    .await;
    assert_eq!(result.status, ResultStatus::Ok);
    assert_eq!(
        data_json(&result),
        "{\"host\": \"127.0.0.1\", \"timeout\": 1.5, \"count\": 2, \"interval\": 0.0, \
         \"target\": \"127.0.0.1\", \"packets_sent\": 2, \"packets_received\": 2, \
         \"packet_loss_percent\": 0.0, \"attempts\": [{\"attempt\": 1, \"status\": \"ok\", \
         \"rtt_ms\": 0.046}, {\"attempt\": 2, \"status\": \"ok\", \"rtt_ms\": 0.046}], \
         \"rtt_min_ms\": 0.046, \"rtt_max_ms\": 0.046, \"rtt_avg_ms\": 0.046, \
         \"rtt_list_ms\": [0.046, 0.046]}"
    );
    assert!(logged_args(&scripts).ends_with("-n -c 1 -W 2 127.0.0.1\n"));
}

#[tokio::test]
async fn a_fake_ping_without_privilege_stops_at_once() {
    let scripts = Scripts::new();
    let ping = fake_ping(
        &scripts,
        "echo 'ping: socktype: SOCK_RAW' >&2\n\
         echo 'ping: socket: Operation not permitted' >&2\nexit 2",
    );
    let result = execute(&linux_executor(&ping), &check("127.0.0.1", "{}")).await;
    assert_eq!(
        result.data["error_type"],
        PyJson::Str("permission_error".to_owned())
    );
    assert_eq!(
        result.data["error_msg"],
        PyJson::Str(format!(
            "{PERMISSION_HINT} (ping: socket: Operation not permitted)"
        ))
    );
    assert_eq!(result.data["packets_sent"], PyJson::Int(1));
    assert_eq!(logged_args(&scripts).lines().count(), 1);
}

#[tokio::test]
async fn a_hanging_ping_is_killed_after_the_grace_period() {
    let scripts = Scripts::new();
    let ping = fake_ping(&scripts, "exec sleep 30");
    let start = tokio::time::Instant::now();
    let result = execute(
        &linux_executor(&ping),
        &check("127.0.0.1", r#"{"count": 1, "timeout": 0.05}"#),
    )
    .await;
    let elapsed = start.elapsed();
    assert!(elapsed >= Duration::from_millis(2000), "{elapsed:?}");
    assert!(elapsed < Duration::from_secs(10), "{elapsed:?}");
    assert_eq!(result.data["error_type"], PyJson::Str("timeout".to_owned()));
    assert_eq!(
        result.data["error_msg"],
        PyJson::Str("No reply from 127.0.0.1 within 0.05s (1 attempt(s))".to_owned())
    );
}

#[tokio::test]
async fn a_name_resolves_before_pinging() {
    let scripts = Scripts::new();
    let ping = fake_ping(&scripts, "echo 'icmp_seq=1 ttl=64 time=1.5 ms'");
    let result = execute(
        &linux_executor(&ping),
        &check("localhost", r#"{"count": 1}"#),
    )
    .await;
    assert_eq!(result.status, ResultStatus::Ok, "{:?}", result.data);
    let target = result.data["target"].as_str().expect("target").to_owned();
    assert!(target == "127.0.0.1" || target == "::1", "{target}");
    assert_eq!(result.data["hostname"], PyJson::Str("localhost".to_owned()));
    assert!(logged_args(&scripts).ends_with(&format!(" {target}\n")));
}

#[tokio::test]
async fn resolver_errors_use_the_python_texts() {
    let addresses = resolve_host("localhost".to_owned())
        .await
        .expect("resolves");
    assert!(!addresses.is_empty());
    let unique: std::collections::HashSet<_> = addresses.iter().collect();
    assert_eq!(unique.len(), addresses.len());
    // An invalid IDNA name fails before any lookup (`idna.IDNAError`,
    // whose text is P2).
    let error = resolve_host("a\u{200d}b.example".to_owned())
        .await
        .expect_err("invalid IDNA name");
    assert!(error.contains("U+200D"), "{error}");
}

#[test]
fn addresses_are_rendered_like_inet_ntop() {
    let v6 = |text: &str| text.parse::<std::net::Ipv6Addr>().unwrap();
    assert_eq!(ipv6_text(v6("::102:304")), "::1.2.3.4");
    assert_eq!(ipv6_text(v6("::1")), "::1");
    assert_eq!(ipv6_text(v6("::")), "::");
    assert_eq!(ipv6_text(v6("::ffff:1.2.3.4")), "::ffff:1.2.3.4");
    assert_eq!(ipv6_text(v6("2001:db8::1")), "2001:db8::1");
    let scoped = SocketAddr::V6(std::net::SocketAddrV6::new(v6("fe80::1"), 0, 0, 4));
    assert_eq!(address_text(scoped), "fe80::1%4");
}

#[test]
fn which_follows_shutil_which() {
    let scripts = Scripts::new();
    let base = scripts.dir.path();
    for dir in ["a", "b", "c"] {
        std::fs::create_dir(base.join(dir)).unwrap();
    }
    // `a/ping` is not executable, `b/ping` is a directory, `c/ping` works.
    std::fs::write(base.join("a/ping"), "").unwrap();
    std::fs::create_dir(base.join("b/ping")).unwrap();
    let good = base.join("c/ping");
    std::fs::write(&good, "#!/bin/sh\n").unwrap();
    std::fs::set_permissions(&good, std::fs::Permissions::from_mode(0o755)).unwrap();
    let entry = |dir: &str| base.join(dir).to_str().unwrap().to_owned();
    let path = OsString::from(format!("{}:{}:{}/", entry("a"), entry("b"), entry("c")));
    assert_eq!(
        which("ping", Some(&path)),
        Some(format!("{}/ping", entry("c")))
    );
    assert_eq!(which("ping", Some(OsStr::new(""))), None);
    assert_eq!(which("ping", Some(OsStr::new(&entry("a")))), None);
    // Without PATH, `os.confstr("CS_PATH")`.
    let expected = CS_PATH
        .split(':')
        .map(|dir| format!("{dir}/sh"))
        .find(|candidate| Path::new(candidate).exists());
    assert_eq!(which("sh", None), expected);
    // Fallbacks need a regular, executable file.
    let fallbacks = [
        base.join("a/ping").to_str().unwrap().to_owned(),
        base.join("b/ping").to_str().unwrap().to_owned(),
        good.to_str().unwrap().to_owned(),
    ];
    let fallbacks: Vec<&str> = fallbacks.iter().map(String::as_str).collect();
    assert_eq!(
        find_binary_in("ping", &fallbacks, Some(OsStr::new(""))),
        Some(good.to_str().unwrap().to_owned())
    );
    assert_eq!(
        find_binary_in("ping", &fallbacks[..2], Some(OsStr::new(""))),
        None
    );
}

#[test]
fn which_skips_entries_that_are_not_utf8() {
    use std::os::unix::ffi::OsStringExt;
    let path = OsString::from_vec(b"/nonexistent/\xff:/bin".to_vec());
    let found = which("sh", Some(&path));
    assert_eq!(found.as_deref(), Some("/bin/sh"));
}
