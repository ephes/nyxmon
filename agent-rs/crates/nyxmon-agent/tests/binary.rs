//! Process-level tests of the `nyxmon-agent` binary (PLAN.md 5.1, 7.3):
//! exit codes of the startup checks, runs against fake servers, SIGTERM,
//! SIGINT and SIGKILL with lease release and stale-lease reclaim, the
//! cleaner, the site observer, and `run-check`.

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

/// How the fake HTTP server answers.
#[derive(Clone, Copy)]
enum Answer {
    /// `200 OK` at once.
    Ok,
    /// Never: the connection is held open.
    Never,
    /// `200 OK` after the delay.
    After(Duration),
}

/// A server answering every request as `answer` says.
fn fake_server_with(answer: Answer) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    std::thread::spawn(move || {
        let mut held = Vec::new();
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { break };
            let delay = match answer {
                Answer::Never => {
                    held.push(stream);
                    continue;
                }
                Answer::Ok => Duration::ZERO,
                Answer::After(delay) => delay,
            };
            std::thread::spawn(move || {
                let mut buffer = [0u8; 4096];
                let _ = stream.read(&mut buffer);
                std::thread::sleep(delay);
                let _ = stream.write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok",
                );
            });
        }
    });
    format!("http://{address}/health")
}

/// A server answering every request with `200 OK` (or never answering).
fn fake_server(respond: bool) -> String {
    fake_server_with(if respond { Answer::Ok } else { Answer::Never })
}

fn wait_until(what: &str, condition: impl Fn() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(20);
    while !condition() {
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        std::thread::sleep(Duration::from_millis(50));
    }
}

fn send_signal(child: &Child, name: &str) {
    let status = Command::new("kill")
        .args([&format!("-{name}"), &child.id().to_string()])
        .status()
        .unwrap();
    assert!(status.success());
}

fn terminate(child: Child) -> Output {
    send_signal(&child, "TERM");
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

fn status_of(db: &Path, check_id: i64) -> String {
    query(
        db,
        &format!("SELECT status FROM health_check WHERE id = {check_id}"),
    )
}

/// Wait for the `Monitoring started` line; the agent holds its lock and
/// its signal handlers are installed from then on.
fn started(child: &mut Child) -> std::thread::JoinHandle<String> {
    let mut stderr = BufReader::new(child.stderr.take().unwrap());
    let mut seen = String::new();
    loop {
        let mut line = String::new();
        assert!(
            stderr.read_line(&mut line).unwrap() > 0,
            "agent ended: {seen}"
        );
        seen.push_str(&line);
        if line.contains("Monitoring started") {
            break;
        }
    }
    // Keep draining, so the agent never blocks on a full pipe.
    std::thread::spawn(move || {
        let mut rest = String::new();
        let _ = stderr.read_to_string(&mut rest);
        seen + &rest
    })
}

#[test]
fn sigint_during_an_in_flight_check_releases_the_claim_without_a_result() {
    // `test_signal_releases_unfinished_claim_without_alert[SIGINT]`.
    let db = migrated_db();
    add_http_check(&db.path, 1, &fake_server(false), r#"{"timeout": 60}"#);
    let path = db.path.to_str().unwrap();
    let mut child = agent(&["--db", path, "--disable-cleaner", "--shadow"])
        .spawn()
        .unwrap();
    let log = started(&mut child);
    wait_until("the claim", || status_of(&db.path, 1) == "processing");
    send_signal(&child, "INT");
    let status = child.wait().unwrap();
    let stderr = log.join().unwrap();
    assert_eq!(status.code(), Some(0), "{stderr}");
    assert!(stderr.contains(" - INFO - Received signal 2, shutting down..."));
    assert!(!stderr.contains("notification check_id="), "{stderr}");
    assert_eq!(status_of(&db.path, 1), "idle");
    assert!(
        query::<i64>(
            &db.path,
            "SELECT next_check_time FROM health_check WHERE id = 1"
        ) > 0
    );
    assert_eq!(
        query::<i64>(&db.path, "SELECT COUNT(*) FROM check_result"),
        0
    );
    assert_eq!(
        query::<i64>(&db.path, "SELECT COUNT(*) FROM collector_incident"),
        0
    );
}

#[test]
fn a_result_finishing_within_the_grace_is_recorded() {
    // `test_result_finishing_during_grace_is_recorded`: the check answers
    // one second after the signal, inside the five-second grace.
    let db = migrated_db();
    add_http_check(
        &db.path,
        1,
        &fake_server_with(Answer::After(Duration::from_millis(1500))),
        "{}",
    );
    let path = db.path.to_str().unwrap();
    let mut child = agent(&["--db", path, "--disable-cleaner", "--shadow"])
        .env("NYXMON_SHUTDOWN_GRACE_SECONDS", "5")
        .spawn()
        .unwrap();
    let log = started(&mut child);
    wait_until("the claim", || status_of(&db.path, 1) == "processing");
    std::thread::sleep(Duration::from_millis(300));
    send_signal(&child, "TERM");
    let status = child.wait().unwrap();
    let stderr = log.join().unwrap();
    assert_eq!(status.code(), Some(0), "{stderr}");
    assert_eq!(
        query::<String>(&db.path, "SELECT status FROM check_result"),
        "ok"
    );
    assert_eq!(status_of(&db.path, 1), "idle");
    assert_eq!(
        query::<i64>(
            &db.path,
            "SELECT processing_started_at FROM health_check WHERE id = 1"
        ),
        0
    );
}

#[test]
fn an_idle_agent_shuts_down_promptly() {
    let db = migrated_db();
    let path = db.path.to_str().unwrap();
    let mut child = agent(&["--db", path, "--interval", "1"])
        .env("NYXMON_SHUTDOWN_GRACE_SECONDS", "5")
        .spawn()
        .unwrap();
    let log = started(&mut child);
    let signalled = Instant::now();
    send_signal(&child, "TERM");
    let status = child.wait().unwrap();
    let stderr = log.join().unwrap();
    assert_eq!(status.code(), Some(0), "{stderr}");
    // At most one interval sleep; the grace applies only to a batch.
    assert!(signalled.elapsed() < Duration::from_secs(3), "{stderr}");
}

#[test]
fn sigkill_leaves_the_claim_and_the_next_start_reclaims_it_after_the_lease() {
    // `test_hard_kill_still_reclaims_and_alerts` with the real binary on
    // both sides of the kill (PLAN.md 7.3).
    let db = migrated_db();
    add_http_check(&db.path, 1, &fake_server(false), r#"{"timeout": 60}"#);
    let path = db.path.to_str().unwrap();
    let mut killed = agent(&["--db", path, "--disable-cleaner", "--shadow"])
        .spawn()
        .unwrap();
    wait_until("the claim", || status_of(&db.path, 1) == "processing");
    killed.kill().unwrap();
    killed.wait().unwrap();
    let claimed_at: i64 = query(
        &db.path,
        "SELECT processing_started_at FROM health_check WHERE id = 1",
    );
    assert!(claimed_at > 0);

    // Within the lease the claim is left alone, and a clean shutdown does
    // not release a claim this process does not own.
    let mut within = agent(&[
        "--db",
        path,
        "--interval",
        "1",
        "--disable-cleaner",
        "--shadow",
    ])
    .spawn()
    .unwrap();
    let log = started(&mut within);
    std::thread::sleep(Duration::from_millis(2500));
    send_signal(&within, "TERM");
    assert_eq!(within.wait().unwrap().code(), Some(0));
    let stderr = log.join().unwrap();
    assert!(!stderr.contains("notification check_id="), "{stderr}");
    assert_eq!(status_of(&db.path, 1), "processing");
    assert_eq!(
        query::<i64>(&db.path, "SELECT COUNT(*) FROM check_result"),
        0
    );

    // Age the persisted lease past the effective lease (900 s by default)
    // instead of waiting for it.
    Connection::open(&db.path)
        .unwrap()
        .execute(
            "UPDATE health_check SET processing_started_at = processing_started_at - 901,
                check_interval = 3600 WHERE id = 1",
            [],
        )
        .unwrap();
    let mut next = agent(&[
        "--db",
        path,
        "--interval",
        "1",
        "--disable-cleaner",
        "--shadow",
    ])
    .spawn()
    .unwrap();
    let log = started(&mut next);
    wait_until("the stale result", || {
        query::<i64>(&db.path, "SELECT COUNT(*) FROM check_result") >= 1
    });
    wait_until("the incident", || {
        query::<i64>(
            &db.path,
            "SELECT COUNT(*) FROM collector_incident
             WHERE incident_key = 'collector:stale_processing_lease' AND alert_count = 1",
        ) == 1
    });
    std::thread::sleep(Duration::from_millis(2500));
    send_signal(&next, "TERM");
    assert_eq!(next.wait().unwrap().code(), Some(0));
    let stderr = log.join().unwrap();

    let data: String = query(&db.path, "SELECT data FROM check_result");
    assert!(
        data.starts_with(r#"{"error_type": "stale_processing_lease", "#),
        "{data}"
    );
    assert_eq!(
        query::<String>(&db.path, "SELECT status FROM check_result"),
        "error"
    );
    // Rescheduled one interval ahead, so the check did not run again.
    assert_eq!(
        query::<i64>(&db.path, "SELECT COUNT(*) FROM check_result"),
        1
    );
    assert_eq!(status_of(&db.path, 1), "idle");
    // Exactly one alert: the batch alert; the check's own first failure is
    // below the default threshold of two.
    let alerts: Vec<&str> = stderr
        .lines()
        .filter(|line| line.contains("notification check_id="))
        .collect();
    assert_eq!(alerts.len(), 1, "{stderr}");
    assert!(
        alerts[0].ends_with(
            " - nyxmon.adapters.notification - ERROR - notification check_id=0 status=error \
             error_type=stale_processing_lease_batch"
        ),
        "{}",
        alerts[0]
    );
}

#[test]
fn the_cleaner_deletes_expired_results_and_sessions() {
    // `cleaner_e2e_test.py::test_cleaner_integration` through the binary.
    let db = migrated_db();
    add_http_check(&db.path, 1, "http://127.0.0.1:1/never", "{}");
    Connection::open(&db.path)
        .unwrap()
        .execute_batch(
            "UPDATE health_check SET disabled = 1;
             INSERT INTO check_result (health_check_id, status, data, created_at) VALUES
                (1, 'ok', '{}', datetime('now', '-3 days')),
                (1, 'ok', '{}', datetime('now', '-2 days')),
                (1, 'ok', '{}', datetime('now', '-1 hours'));
             INSERT INTO django_session (session_key, session_data, expire_date) VALUES
                ('old', 'x', datetime('now', '-1 days')),
                ('new', 'x', datetime('now', '+1 days'));",
        )
        .unwrap();
    let path = db.path.to_str().unwrap();
    let mut child = agent(&["--db", path, "--batch-size", "1"]).spawn().unwrap();
    let log = started(&mut child);
    wait_until("the cleanup", || {
        query::<i64>(&db.path, "SELECT COUNT(*) FROM check_result") == 1
    });
    wait_until("the session cleanup", || {
        query::<i64>(&db.path, "SELECT COUNT(*) FROM django_session") == 1
    });
    send_signal(&child, "TERM");
    assert_eq!(child.wait().unwrap().code(), Some(0));
    let stderr = log.join().unwrap();
    assert_eq!(
        query::<String>(&db.path, "SELECT session_key FROM django_session"),
        "new"
    );
    assert!(stderr.contains("Cleaned up"), "{stderr}");
}

fn run_check(args: &[&str]) -> (i32, String, String) {
    let output = agent(&[&["run-check"], args].concat()).output().unwrap();
    (
        output.status.code().unwrap_or(-1),
        String::from_utf8(output.stdout).unwrap(),
        String::from_utf8_lossy(&output.stderr).into_owned(),
    )
}

#[test]
fn run_check_prints_one_result_without_writing_the_database() {
    let db = migrated_db();
    let url = fake_server(true);
    add_http_check(&db.path, 1, &url, r#"{"expected_status": 200}"#);
    add_http_check(&db.path, 2, &url, "{}");
    Connection::open(&db.path)
        .unwrap()
        .execute_batch("UPDATE health_check SET disabled = 1, check_type = 'legacy' WHERE id = 2")
        .unwrap();
    let path = db.path.to_str().unwrap();

    let (code, stdout, stderr) = run_check(&["--db", path, "--check-id", "1"]);
    assert_eq!(code, 0, "{stderr}");
    let line = stdout.strip_suffix('\n').expect("one line");
    assert!(
        line.starts_with(r#"{"check_id": 1, "check_type": "http", "status": "ok", "data": {"#),
        "{line}"
    );
    assert!(!line.contains('\n'));
    // Disabled checks run too (the shadow copy disables the mail checks).
    let (code, stdout, _) = run_check(&["--db", path, "--check-id", "2"]);
    assert_eq!(code, 0);
    assert!(
        stdout.starts_with(
            r#"{"check_id": 2, "check_type": "legacy", "status": "error", "data": {"error_type": "unknown_check_type", "#
        ),
        "{stdout}"
    );
    // Even while an agent holds the lock (no lock is taken).
    let mut running = agent(&["--db", path, "--disable-cleaner"]).spawn().unwrap();
    let log = started(&mut running);
    let (code, _, stderr) = run_check(&["--db", path, "--check-id", "1"]);
    assert_eq!(code, 0, "{stderr}");
    send_signal(&running, "TERM");
    assert_eq!(running.wait().unwrap().code(), Some(0));
    drop(log.join());

    let (code, stdout, stderr) = run_check(&["--db", path, "--check-id", "3"]);
    assert_eq!((code, stdout.as_str()), (1, ""));
    assert!(
        stderr.contains(&format!(" - ERROR - check 3 not found in {path}")),
        "{stderr}"
    );
    let (code, _, stderr) = run_check(&["--db", "missing.sqlite3", "--check-id", "1"]);
    assert_eq!(code, 1);
    assert!(stderr.contains("Database file not found: missing.sqlite3"));
    assert_eq!(run_check(&["--db", path]).0, 2);
    assert_eq!(run_check(&["--db", path, "--check-id", "x"]).0, 2);

    // The running agent wrote its results; run-check wrote none of its own.
    let rows: i64 = query(&db.path, "SELECT COUNT(*) FROM check_result");
    let agent_rows: i64 = query(
        &db.path,
        "SELECT COUNT(*) FROM check_result WHERE health_check_id = 1",
    );
    assert_eq!(rows, agent_rows);
    assert!(rows <= 1);
}

#[test]
fn run_check_leaves_an_untouched_database_unmodified() {
    let db = migrated_db();
    add_http_check(&db.path, 1, &fake_server(true), "{}");
    let before = std::fs::read(&db.path).unwrap();
    let path = db.path.to_str().unwrap();
    let (code, _, stderr) = run_check(&["--db", path, "--check-id", "1"]);
    assert_eq!(code, 0, "{stderr}");
    assert_eq!(std::fs::read(&db.path).unwrap(), before);
    assert!(!db.path.with_extension("sqlite3.agent.lock").exists());
}
