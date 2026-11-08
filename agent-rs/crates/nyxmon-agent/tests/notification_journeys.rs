//! Actual process-death boundaries, driven through ResultHandler and SQLite.
use std::fs::OpenOptions;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, mpsc};
use std::time::Duration;

use nyxmon_agent::results::ResultHandler;
use nyxmon_core::env::Env;
use nyxmon_core::log::{RecordingSink, WarnOnce};
use nyxmon_core::model::{CheckResult, NotificationState, ResultStatus};
use nyxmon_core::pyjson::{PyDict, PyJson};
use nyxmon_notify::{Delivery, Notifier, NotifyFuture};
use nyxmon_store::{Store, read_check};
use rusqlite::Connection;

const T0: i64 = 1_800_000_000;
const SCHEMA: &str = include_str!("../../../fixtures/schema/schema.sql");

struct FakeTransport {
    mode: String,
    accepted: PathBuf,
}

fn boundary() {
    println!("JOURNEY_BOUNDARY");
    std::io::stdout().flush().unwrap();
    // Parent owns the pipe and kills this process without releasing it.
    let mut line = String::new();
    std::io::stdin().read_line(&mut line).unwrap();
    panic!("boundary was unexpectedly released");
}

impl Notifier for FakeTransport {
    fn notify_check_failed<'a>(
        &'a self,
        _: &'a nyxmon_core::model::Check,
        _: &'a CheckResult,
    ) -> NotifyFuture<'a> {
        Box::pin(async move {
            if self.mode == "before" {
                boundary();
            }
            let mut accepted = OpenOptions::new()
                .create(true)
                .append(true)
                .open(&self.accepted)
                .unwrap();
            writeln!(accepted, "accepted").unwrap();
            accepted.sync_all().unwrap();
            if self.mode == "after" {
                boundary();
            }
            Ok(Delivery::Delivered)
        })
    }
}

fn handler(
    store: Store,
    mode: &str,
    accepted: &Path,
    now: i64,
    threshold: &str,
) -> ResultHandler<Store> {
    let sink = Arc::new(RecordingSink::default());
    let env = Arc::new(Env::from_map(
        [
            ("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", threshold),
            ("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "86400"),
            ("NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS", "60"),
            ("NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS", "3600"),
        ],
        sink.clone(),
    ));
    ResultHandler::new(
        store,
        Arc::new(FakeTransport {
            mode: mode.to_owned(),
            accepted: accepted.to_owned(),
        }),
        env,
        Arc::new(WarnOnce::new(sink)),
    )
    .with_clock(Arc::new(move || now))
}

fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
}

// Invoked explicitly by the parent; ignored in ordinary cargo test runs.
#[test]
#[ignore = "subprocess entry point for notification crash journeys"]
fn process_worker() {
    let Some(path) = std::env::var_os("NYXMON_JOURNEY_DB") else {
        return; // Only the crash parent supplies the worker's inputs.
    };
    let path = PathBuf::from(path);
    let accepted = PathBuf::from(std::env::var_os("NYXMON_JOURNEY_ACCEPTED").unwrap());
    let mode = std::env::var("NYXMON_JOURNEY_MODE").unwrap();
    let now: i64 = std::env::var("NYXMON_JOURNEY_NOW")
        .unwrap()
        .parse()
        .unwrap();
    let immediate = std::env::var("NYXMON_JOURNEY_IMMEDIATE").unwrap() == "true";
    let check = read_check(&path, 1).unwrap().unwrap();
    let mut data = PyDict::new();
    if immediate {
        data.insert("notification_immediate".to_owned(), PyJson::Bool(true));
    }
    runtime().block_on(async {
        let handler = handler(Store::open(&path).unwrap(), &mode, &accepted, now, "1");
        handler
            .add_check_result(
                &check,
                CheckResult::new(1, ResultStatus::Error, data),
                false,
            )
            .await
            .unwrap();
    });
}

struct Worker(Child);
impl Drop for Worker {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn worker(path: &Path, accepted: &Path, mode: &str, now: i64, immediate: bool) -> Command {
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
        .args(["--exact", "process_worker", "--ignored", "--nocapture"])
        .env("NYXMON_JOURNEY_DB", path)
        .env("NYXMON_JOURNEY_ACCEPTED", accepted)
        .env("NYXMON_JOURNEY_MODE", mode)
        .env("NYXMON_JOURNEY_NOW", now.to_string())
        .env("NYXMON_JOURNEY_IMMEDIATE", immediate.to_string())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    command
}

fn die_at_boundary(path: &Path, accepted: &Path, mode: &str, now: i64, immediate: bool) {
    let mut child = Worker(
        worker(path, accepted, mode, now, immediate)
            .spawn()
            .unwrap(),
    );
    let stdout = child.0.stdout.take().unwrap();
    let (tx, rx) = mpsc::channel();
    let reader = std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            if line.unwrap().contains("JOURNEY_BOUNDARY") {
                tx.send(()).unwrap();
                return;
            }
        }
    });
    let reached = rx.recv_timeout(Duration::from_secs(30));
    child.0.kill().unwrap();
    assert!(!child.0.wait().unwrap().success());
    reader.join().unwrap();
    reached.expect("worker never reached the requested boundary");
}

fn sample(path: &Path, accepted: &Path, now: i64, immediate: bool) {
    let output = worker(path, accepted, "normal", now, immediate)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
}

fn state(path: &Path) -> NotificationState {
    runtime()
        .block_on(Store::open(path).unwrap().get_notification_state(1))
        .unwrap()
}

fn accepted_count(path: &Path) -> usize {
    std::fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .count()
}

#[test]
fn notification_intents_survive_death_before_send_and_after_acceptance() {
    for mode in ["before", "after"] {
        for immediate in [false, true] {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("db.sqlite3");
            let accepted = dir.path().join("accepted.txt");
            let db = Connection::open(&path).unwrap();
            db.execute_batch(SCHEMA).unwrap();
            db.execute("INSERT INTO service (id,name) VALUES (1,'synthetic')", [])
                .unwrap();
            db.execute("INSERT INTO health_check (id,service_id,name,check_type,url,check_interval,status,next_check_time,processing_started_at,disabled,data) VALUES (1,1,'synthetic','http','https://example.invalid',300,'idle',0,0,0,'{}')", []).unwrap();
            die_at_boundary(&path, &accepted, mode, T0, immediate);
            let first = state(&path);
            assert_eq!((first.attempt_seq, first.attempt_at), (1, T0));
            assert_eq!(accepted_count(&accepted), usize::from(mode == "after"));
            let results: i64 = db
                .query_row("SELECT count(*) FROM check_result", [], |row| row.get(0))
                .unwrap();
            assert_eq!(results, 1, "intent and sample committed before send");

            sample(&path, &accepted, T0 + 59, immediate);
            assert_eq!(state(&path).attempt_seq, 1);
            die_at_boundary(&path, &accepted, "after", T0 + 60, immediate);
            let replacement = state(&path);
            assert_eq!(
                (replacement.attempt_seq, replacement.attempt_at),
                (2, T0 + 60)
            );
            assert_eq!(
                replacement.last_immediate_at,
                if immediate { T0 + 60 } else { 0 }
            );
            assert_eq!(
                replacement.last_notified_at,
                if immediate { 0 } else { T0 + 60 }
            );
            assert!(
                !runtime()
                    .block_on(
                        Store::open(&path)
                            .unwrap()
                            .acknowledge_notification_attempt(1, 1)
                    )
                    .unwrap()
            );
            assert_eq!(state(&path), replacement);

            sample(&path, &accepted, T0 + 119, immediate);
            assert_eq!(state(&path).attempt_seq, 2);
            sample(&path, &accepted, T0 + 120, immediate);
            assert_eq!((state(&path).attempt_seq, state(&path).attempt_at), (3, 0));
            for offset in [121, 179, 180, 300] {
                sample(&path, &accepted, T0 + offset, immediate);
            }
            assert_eq!(
                state(&path).attempt_seq,
                3,
                "acknowledged retries cannot loop"
            );
            assert_eq!(accepted_count(&accepted), 2 + usize::from(mode == "after"));
        }
    }
}

// Python verifies this projection against real historical migrations; the local
// live-handoff command also passes the actual migrated file to this same oracle.
#[test]
fn migrated_reminder_state() {
    let directory = tempfile::tempdir().unwrap();
    let (path, now) = match std::env::var_os("NYXMON_MIGRATED_DB") {
        Some(path) => (
            PathBuf::from(path),
            std::env::var("NYXMON_MIGRATION_NOW")
                .unwrap()
                .parse::<i64>()
                .unwrap(),
        ),
        None => {
            let path = directory.path().join("migrated.sqlite3");
            let db = Connection::open(&path).unwrap();
            db.execute_batch(SCHEMA).unwrap();
            db.execute_batch(include_str!(
                "../../../fixtures/notification_reminder_upgrade.sql"
            ))
            .unwrap();
            (path, T0)
        }
    };
    let accepted = path.with_extension("accepted.txt");
    runtime().block_on(async {
        let store = Store::open(&path).unwrap();
        let established = store.get_notification_state(1).await.unwrap();
        let unalerted = store.get_notification_state(2).await.unwrap();
        assert_eq!(
            (established.first_failure_at, established.last_notified_at),
            (now, now)
        );
        assert_eq!(
            (unalerted.first_failure_at, unalerted.last_notified_at),
            (now, 0)
        );
        let old_check = read_check(&path, 1).unwrap().unwrap();
        let new_check = read_check(&path, 2).unwrap().unwrap();
        let fail = |id| CheckResult::new(id, ResultStatus::Error, PyDict::new());
        for offset in [1, 86_399] {
            let handler = handler(store.clone(), "normal", &accepted, now + offset, "3");
            assert!(
                !handler
                    .add_check_result(&old_check, fail(1), false)
                    .await
                    .unwrap()
            );
            assert_eq!(
                store
                    .get_notification_state(1)
                    .await
                    .unwrap()
                    .first_failure_at,
                now
            );
        }
        let first = handler(store.clone(), "normal", &accepted, now + 1, "3");
        assert!(
            !first
                .add_check_result(&new_check, fail(2), false)
                .await
                .unwrap()
        );
        assert_eq!(
            store.get_notification_state(2).await.unwrap().failure_count,
            2
        );
        let threshold = handler(store.clone(), "normal", &accepted, now + 2, "3");
        assert!(
            threshold
                .add_check_result(&new_check, fail(2), false)
                .await
                .unwrap()
        );
        assert_eq!(
            store
                .get_notification_state(2)
                .await
                .unwrap()
                .first_failure_at,
            now
        );
        let due = handler(store.clone(), "normal", &accepted, now + 86_400, "3");
        assert!(
            due.add_check_result(&old_check, fail(1), false)
                .await
                .unwrap()
        );
        assert_eq!(store.get_notification_state(1).await.unwrap().attempt_at, 0);
    });
    assert_eq!(accepted_count(&accepted), 2);
}
