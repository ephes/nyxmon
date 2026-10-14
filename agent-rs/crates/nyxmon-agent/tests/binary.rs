//! Process-level tests of the `nyxmon-agent` binary (PLAN.md 5.1, 7.3 for
//! the parts S9 owns): exit codes of the startup checks, a run against a
//! fake HTTP server, and SIGTERM handling with lease release.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output, Stdio};
use std::time::{Duration, Instant};

use rusqlite::{Connection, params};

const SCHEMA_SQL: &str = include_str!("../../../fixtures/schema/schema.sql");
const BINARY: &str = env!("CARGO_BIN_EXE_nyxmon-agent");

struct Db {
    _dir: tempfile::TempDir,
    path: PathBuf,
}

fn migrated_db() -> Db {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("db.sqlite3");
    let connection = Connection::open(&path).unwrap();
    connection.execute_batch(SCHEMA_SQL).unwrap();
    connection
        .execute("INSERT INTO service (id, name) VALUES (1, 'service')", [])
        .unwrap();
    Db { _dir: dir, path }
}

fn add_http_check(db: &Path, check_id: i64, url: &str, data: &str) {
    Connection::open(db)
        .unwrap()
        .execute(
            "INSERT INTO health_check (id, service_id, name, check_type, url, check_interval,
                next_check_time, processing_started_at, status, disabled, data)
             VALUES (?, 1, 'fake', 'http', ?, 300, 0, 0, 'idle', 0, ?)",
            params![check_id, url, data],
        )
        .unwrap();
}

fn query<T: rusqlite::types::FromSql>(db: &Path, sql: &str) -> T {
    Connection::open(db)
        .unwrap()
        .query_row(sql, [], |row| row.get(0))
        .unwrap()
}

fn agent(args: &[&str]) -> Command {
    let mut command = Command::new(BINARY);
    command
        .args(args)
        .env_remove("TELEGRAM_BOT_TOKEN")
        .env_remove("TELEGRAM_CHAT_ID")
        .env("NYXMON_SHUTDOWN_GRACE_SECONDS", "0")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    command
}

fn run(args: &[&str]) -> (i32, String) {
    let output = agent(args).output().unwrap();
    (
        output.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&output.stderr).into_owned(),
    )
}

/// A server answering every request with `200 OK` (or never answering).
fn fake_server(respond: bool) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    std::thread::spawn(move || {
        let mut held = Vec::new();
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { break };
            if !respond {
                held.push(stream);
                continue;
            }
            let mut buffer = [0u8; 4096];
            let _ = stream.read(&mut buffer);
            let _ = stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok");
        }
    });
    format!("http://{address}/health")
}

fn wait_until(what: &str, condition: impl Fn() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(20);
    while !condition() {
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        std::thread::sleep(Duration::from_millis(50));
    }
}

fn terminate(child: Child) -> Output {
    let status = Command::new("kill")
        .args(["-TERM", &child.id().to_string()])
        .status()
        .unwrap();
    assert!(status.success());
    child.wait_with_output().unwrap()
}

#[test]
fn a_missing_database_exits_1() {
    let (code, stderr) = run(&["--db", "./missing//db.sqlite3"]);
    assert_eq!(code, 1);
    assert!(
        stderr.contains(
            " - nyxmon.entrypoints.cli - ERROR - Database file not found: missing/db.sqlite3\n"
        ),
        "{stderr}"
    );
}

#[test]
fn an_old_schema_exits_1_and_check_schema_reports_it() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("old.sqlite3");
    Connection::open(&path)
        .unwrap()
        .execute_batch("CREATE TABLE health_check (id INTEGER PRIMARY KEY)")
        .unwrap();
    let path = path.to_str().unwrap();
    let (code, stderr) = run(&["--db", path]);
    assert_eq!(code, 1);
    assert!(stderr.contains("run `manage.py migrate`"), "{stderr}");
    let (code, stderr) = run(&["--db", path, "--check-schema"]);
    assert_eq!(code, 1);
    assert!(stderr.contains("database schema is older than the agent contract"));

    let db = migrated_db();
    let (code, stderr) = run(&["--db", db.path.to_str().unwrap(), "--check-schema"]);
    assert_eq!(code, 0, "{stderr}");
    assert!(stderr.contains("database schema satisfies the agent contract"));
    // Read-only: no lock file is created.
    assert!(!db.path.with_extension("sqlite3.agent.lock").exists());
}

#[test]
fn usage_errors_and_conflicting_flags_are_refused() {
    let db = migrated_db();
    let path = db.path.to_str().unwrap();
    assert_eq!(run(&["--db", path, "--log-level", "VERBOSE"]).0, 2);
    let (code, stderr) = run(&["--db", path, "--shadow", "--enable-telegram"]);
    assert_eq!(code, 1);
    assert!(stderr.contains("--shadow cannot be combined with --enable-telegram"));
    let (code, stderr) = run(&["--db", path, "--batch-size", "0"]);
    assert_eq!(code, 1);
    assert!(
        stderr.contains(
            "Error running monitoring services\nValueError: batch_size must be at least 1"
        ),
        "{stderr}"
    );
}

#[test]
fn version_prints_the_package_version_and_the_git_commit() {
    let output = agent(&["--version"]).output().unwrap();
    assert!(output.status.success());
    assert!(output.stderr.is_empty());
    let stdout = String::from_utf8(output.stdout).unwrap();
    let line = stdout.strip_suffix('\n').expect("one line");
    let rest = line
        .strip_prefix(&format!("nyxmon-agent {} (", env!("CARGO_PKG_VERSION")))
        .unwrap_or_else(|| panic!("unexpected --version output {line:?}"));
    let commit = rest.strip_suffix(')').expect("closing parenthesis");
    assert!(
        commit == "unknown"
            || ((7..=40).contains(&commit.len())
                && commit
                    .bytes()
                    .all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))),
        "{commit:?}"
    );
    // build.rs exports the same value to this test target.
    assert_eq!(commit, env!("NYXMON_AGENT_GIT_COMMIT"));
}

#[test]
fn a_second_agent_on_the_same_database_exits_1() {
    let db = migrated_db();
    let path = db.path.to_str().unwrap();
    let mut first = agent(&["--db", path, "--disable-cleaner"]).spawn().unwrap();
    // Wait until the first agent is running (it holds the lock by then).
    let mut lines = BufReader::new(first.stderr.take().unwrap()).lines();
    loop {
        let line = lines.next().expect("first agent ended").unwrap();
        if line.contains("Monitoring started") {
            break;
        }
    }
    let lock = format!("{path}.agent.lock");
    let (code, stderr) = run(&["--db", path]);
    assert_eq!(code, 1);
    assert!(
        stderr.contains(&format!(
            "another nyxmon agent holds {lock}; refusing to start"
        )),
        "{stderr}"
    );
    let output = terminate(first);
    assert_eq!(output.status.code(), Some(0));
}

#[test]
fn the_agent_runs_checks_and_shuts_down_on_sigterm() {
    let db = migrated_db();
    add_http_check(&db.path, 1, &fake_server(true), "{}");
    add_http_check(&db.path, 2, "http://127.0.0.1:1/x", r#"{"type": "legacy"}"#);
    Connection::open(&db.path)
        .unwrap()
        .execute(
            "UPDATE health_check SET check_type = 'legacy' WHERE id = 2",
            [],
        )
        .unwrap();
    let path = db.path.to_str().unwrap();
    let child = agent(&["--db", path, "--interval", "1", "--shadow"])
        .spawn()
        .unwrap();
    wait_until("two results", || {
        query::<i64>(&db.path, "SELECT COUNT(*) FROM check_result") >= 2
    });
    let output = terminate(child);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(0), "{stderr}");
    for line in [
        "Initializing monitoring services with database: ",
        " - INFO - database schema satisfies the agent contract (journal_mode=delete)",
        "Found 1 check(s) with unregistered types: {'legacy'}",
        " - nyxmon.entrypoints.cli - INFO - Monitoring started with 1s check interval",
        " - nyxmon.entrypoints.cli - INFO - Received signal 15, shutting down...",
    ] {
        assert!(stderr.contains(line), "missing {line:?} in {stderr}");
    }
    // `%(asctime)s - %(name)s - %(levelname)s - %(message)s`.
    let started = stderr
        .lines()
        .find(|line| line.contains("Monitoring started"))
        .unwrap();
    let pattern = "dddd-dd-dd dd:dd:dd,ddd - ";
    assert!(
        started.len() > pattern.len()
            && pattern
                .chars()
                .zip(started.chars())
                .all(|(p, c)| { if p == 'd' { c.is_ascii_digit() } else { p == c } }),
        "{started}"
    );
    assert_eq!(
        query::<String>(
            &db.path,
            "SELECT data FROM check_result WHERE health_check_id = 1 LIMIT 1"
        ),
        "{}"
    );
    assert_eq!(
        query::<String>(&db.path, "SELECT status FROM health_check WHERE id = 1"),
        "idle"
    );
}

#[test]
fn sigterm_during_an_in_flight_check_releases_the_claim_without_a_result() {
    let db = migrated_db();
    add_http_check(&db.path, 1, &fake_server(false), r#"{"timeout": 60}"#);
    let path = db.path.to_str().unwrap();
    let child = agent(&["--db", path, "--disable-cleaner"]).spawn().unwrap();
    wait_until("the claim", || {
        query::<String>(&db.path, "SELECT status FROM health_check WHERE id = 1") == "processing"
    });
    let started = Instant::now();
    let output = terminate(child);
    assert_eq!(output.status.code(), Some(0));
    assert!(started.elapsed() < Duration::from_secs(5));
    assert_eq!(
        query::<String>(&db.path, "SELECT status FROM health_check WHERE id = 1"),
        "idle"
    );
    assert_eq!(
        query::<i64>(
            &db.path,
            "SELECT processing_started_at FROM health_check WHERE id = 1"
        ),
        0
    );
    assert_eq!(
        query::<i64>(&db.path, "SELECT COUNT(*) FROM check_result"),
        0
    );
}

/// The persisted `site:connectivity` payload, if the row exists.
fn site_payload(db: &Path) -> Option<String> {
    Connection::open(db)
        .unwrap()
        .query_row(
            "SELECT payload FROM collector_incident WHERE incident_key = 'site:connectivity'",
            [],
            |row| row.get(0),
        )
        .ok()
}

#[test]
fn the_site_observer_probes_local_targets_and_persists_each_round() {
    // PLAN.md 6, S11: `observe` mode against local probe targets. IPv4 is a
    // refused port, so the path goes `failing` in the first round and `down`
    // in the second; the DNS name resolves; IPv6 is unobserved.
    let db = migrated_db();
    add_http_check(
        &db.path,
        1,
        &fake_server(true),
        r#"{"site_dependency": {"requires": ["ipv6"]}}"#,
    );
    let path = db.path.to_str().unwrap();
    let child = agent(&["--db", path, "--interval", "1", "--shadow"])
        .env("NYXMON_SITE_CONNECTIVITY_MODE", "observe")
        .env("NYXMON_SITE_PROBE_INTERVAL_SECONDS", "15")
        .env("NYXMON_SITE_PROBE_TIMEOUT_SECONDS", "1")
        .env("NYXMON_SITE_PROBE_IPV4_TARGETS", "127.0.0.1:1")
        .env("NYXMON_SITE_PROBE_IPV6_TARGETS", "")
        .env("NYXMON_SITE_PROBE_DNS_NAMES", "localhost")
        .spawn()
        .unwrap();
    wait_until("the first round", || {
        site_payload(&db.path).is_some_and(|payload| payload.contains(r#""state": "failing""#))
    });
    let first = site_payload(&db.path).unwrap();
    assert!(
        first.starts_with(
            r#"{"incident_type": "site_connectivity", "version": 1, "phase": "idle", "incident_id": 0, "observed_at": "#
        ),
        "{first}"
    );
    assert!(
        first.contains(
            r#""ipv4": {"state": "failing", "down_since": 0, "recovered_at": 0, "release_at": 0, "last_release_at": 0}"#
        ),
        "{first}"
    );
    assert!(first.contains(r#""dns": {"state": "up""#), "{first}");
    assert!(!first.contains("ipv6"), "{first}");
    wait_until("the second round", || {
        site_payload(&db.path).is_some_and(|payload| payload.contains(r#""phase": "active""#))
    });
    let second = site_payload(&db.path).unwrap();
    assert!(second.contains(r#""ipv4": {"state": "down""#), "{second}");
    assert!(second.contains(r#""dns": {"state": "up""#), "{second}");

    let output = terminate(child);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(0), "{stderr}");
    for line in [
        " - nyxmon.bootstrap - INFO - site connectivity observation is observe\n",
        " - nyxmon.adapters.site_connectivity - WARNING - check_id=1 requires the unobserved \
         path(s) ipv6",
        " - nyxmon.adapters.site_connectivity - INFO - site connectivity incident ",
    ] {
        assert!(stderr.contains(line), "missing {line:?} in {stderr}");
    }
    // PLAN.md 5.1: the mode line comes after the schema report and before
    // the check type validation.
    let position = |text: &str| stderr.find(text).unwrap_or(usize::MAX);
    assert!(position("journal_mode=") < position("site connectivity observation is"));
    assert!(
        position("site connectivity observation is")
            < position("Validated 1 check(s) - all types registered")
    );
}

#[test]
fn site_connectivity_off_starts_no_observer() {
    let db = migrated_db();
    add_http_check(
        &db.path,
        1,
        &fake_server(true),
        r#"{"site_dependency": "internet"}"#,
    );
    let path = db.path.to_str().unwrap();
    let child = agent(&["--db", path, "--interval", "1", "--shadow"])
        .env("NYXMON_SITE_CONNECTIVITY_MODE", "off")
        .spawn()
        .unwrap();
    wait_until("a result", || {
        query::<i64>(&db.path, "SELECT COUNT(*) FROM check_result") >= 1
    });
    let output = terminate(child);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(0), "{stderr}");
    assert!(!stderr.contains("site connectivity"), "{stderr}");
    assert!(site_payload(&db.path).is_none());
}
