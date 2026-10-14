//! Test helpers: databases from the Django schema fixture, a scripted
//! notifier, a manual clock.

use std::path::PathBuf;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Mutex};

use nyxmon_core::PyJson;
use nyxmon_core::env::Env;
use nyxmon_core::log::{RecordingSink, WarnOnce};
use nyxmon_core::model::{Check, CheckResult, NotificationState, ResultStatus};
use nyxmon_core::pyjson::{PyDict, loads};
use nyxmon_notify::{Delivery, Notifier, NotifyFuture, NotifyRaised};
use nyxmon_store::Store;
use rusqlite::{Connection, OptionalExtension, params};

use crate::results::{Clock, ResultHandler};

pub const SCHEMA_SQL: &str = include_str!("../../../fixtures/schema/schema.sql");

/// A temporary database migrated to the committed Django schema.
pub struct TestDb {
    _dir: tempfile::TempDir,
    pub path: PathBuf,
}

impl TestDb {
    pub fn new() -> TestDb {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("db.sqlite3");
        let connection = Connection::open(&path).unwrap();
        connection.execute_batch(SCHEMA_SQL).unwrap();
        connection
            .execute("INSERT INTO service (id, name) VALUES (1, 'service')", [])
            .unwrap();
        TestDb { _dir: dir, path }
    }

    pub fn store(&self) -> Store {
        Store::open(&self.path).unwrap()
    }

    pub fn raw(&self) -> Connection {
        let connection = Connection::open(&self.path).unwrap();
        connection
            .execute_batch("PRAGMA foreign_keys = ON")
            .unwrap();
        connection
    }

    /// Insert an idle check row like [`check`] describes.
    pub fn insert(&self, check: &Check) {
        self.raw()
            .execute(
                "INSERT INTO health_check (id, service_id, name, check_type, url, check_interval,
                    next_check_time, processing_started_at, status, disabled, data)
                 VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                params![
                    check.check_id,
                    check.name,
                    check.check_type,
                    check.url,
                    check.check_interval,
                    check.next_check_time,
                    check.processing_started_at,
                    check.status,
                    check.disabled,
                    check.data.to_json_string().unwrap(),
                ],
            )
            .unwrap();
    }

    pub fn set_state(&self, check_id: i64, state: NotificationState) {
        let [a, b, c, d, e, f, g, h] = state.as_row();
        self.raw()
            .execute(
                "INSERT OR REPLACE INTO check_notification_state (check_id, failure_count,
                    last_attempt_count, last_immediate_at, last_notified_at, first_failure_at,
                    held_since, attempt_seq, attempt_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                params![check_id, a, b, c, d, e, f, g, h],
            )
            .unwrap();
    }

    /// The stored state (all zeros when there is no row).
    pub fn state(&self, check_id: i64) -> NotificationState {
        let row: Option<Vec<Option<i64>>> = self
            .raw()
            .query_row(
                "SELECT failure_count, last_attempt_count, last_immediate_at, last_notified_at,
                    first_failure_at, held_since, attempt_seq, attempt_at
                 FROM check_notification_state WHERE check_id = ?",
                [check_id],
                |row| (0..8).map(|index| row.get(index)).collect(),
            )
            .optional()
            .unwrap();
        NotificationState::from_row(row.as_deref())
    }

    /// `(status, next_check_time, processing_started_at)` of a check.
    pub fn schedule(&self, check_id: i64) -> (String, i64, i64) {
        self.raw()
            .query_row(
                "SELECT status, next_check_time, processing_started_at FROM health_check
                 WHERE id = ?",
                [check_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap()
    }

    /// `(health_check_id, status, data)` of every result, by id.
    pub fn results(&self) -> Vec<(i64, String, String)> {
        let connection = self.raw();
        let mut statement = connection
            .prepare("SELECT health_check_id, status, data FROM check_result ORDER BY id")
            .unwrap();
        statement
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap()
    }

    pub fn execute(&self, sql: &str) {
        self.raw().execute_batch(sql).unwrap();
    }
}

/// An idle `http` check (`_build_check`).
pub fn check(check_id: i64) -> Check {
    Check {
        check_id,
        service_id: 1,
        name: format!("Check {check_id}"),
        check_type: "http".to_owned(),
        url: "https://example.test/health".to_owned(),
        check_interval: 300,
        next_check_time: 0,
        processing_started_at: 0,
        status: "idle".to_owned(),
        disabled: false,
        data: PyJson::dict(),
        claim_started_at: 0,
    }
}

pub fn dict(json: &str) -> PyDict {
    loads(json).unwrap().into_dict().unwrap()
}

pub fn result(check_id: i64, status: ResultStatus, data: &str) -> CheckResult {
    CheckResult::new(check_id, status, dict(data))
}

/// A manual clock (`Clock` of the Python tests).
#[derive(Clone)]
pub struct ManualClock(Arc<AtomicI64>);

impl ManualClock {
    pub fn new(start: i64) -> Self {
        ManualClock(Arc::new(AtomicI64::new(start)))
    }

    pub fn now(&self) -> i64 {
        self.0.load(Ordering::SeqCst)
    }

    pub fn advance(&self, seconds: i64) {
        self.0.fetch_add(seconds, Ordering::SeqCst);
    }

    pub fn clock(&self) -> Clock {
        let inner = self.0.clone();
        Arc::new(move || inner.load(Ordering::SeqCst))
    }
}

type Outcome = Box<dyn Fn(usize) -> Result<Delivery, NotifyRaised> + Send + Sync>;

/// Records every send and returns a scripted outcome
/// (`ScriptedNotifier`); with a store it also records the notification
/// state at send time.
pub struct ScriptedNotifier {
    calls: Mutex<Vec<(Check, CheckResult)>>,
    states_at_send: Mutex<Vec<NotificationState>>,
    outcome: Mutex<Outcome>,
    store: Option<Store>,
}

impl ScriptedNotifier {
    pub fn new(store: Option<Store>) -> Arc<Self> {
        Arc::new(ScriptedNotifier {
            calls: Mutex::new(Vec::new()),
            states_at_send: Mutex::new(Vec::new()),
            outcome: Mutex::new(Box::new(|_| Ok(Delivery::Delivered))),
            store,
        })
    }

    pub fn set_outcome(
        &self,
        outcome: impl Fn(usize) -> Result<Delivery, NotifyRaised> + Send + Sync + 'static,
    ) {
        *self.outcome.lock().unwrap() = Box::new(outcome);
    }

    pub fn always(&self, delivery: Delivery) {
        self.set_outcome(move |_| Ok(delivery));
    }

    pub fn count(&self) -> usize {
        self.calls.lock().unwrap().len()
    }

    pub fn calls(&self) -> Vec<(Check, CheckResult)> {
        self.calls.lock().unwrap().clone()
    }

    pub fn states_at_send(&self) -> Vec<NotificationState> {
        self.states_at_send.lock().unwrap().clone()
    }
}

impl Notifier for ScriptedNotifier {
    fn notify_check_failed<'a>(
        &'a self,
        check: &'a Check,
        result: &'a CheckResult,
    ) -> NotifyFuture<'a> {
        Box::pin(async move {
            let call = {
                let mut calls = self.calls.lock().unwrap();
                calls.push((check.clone(), result.clone()));
                calls.len()
            };
            if let Some(store) = &self.store {
                let state = store.get_notification_state(check.check_id).await.unwrap();
                self.states_at_send.lock().unwrap().push(state);
            }
            (self.outcome.lock().unwrap())(call)
        })
    }
}

/// An environment of fixed variables, warnings recorded.
pub fn env(vars: &[(&str, &str)]) -> (Arc<Env>, Arc<RecordingSink>) {
    let sink = Arc::new(RecordingSink::default());
    let env = Env::from_map(vars.iter().copied(), sink.clone());
    (Arc::new(env), sink)
}

/// A handler on `store` with the given variables and clock; no suppression
/// fetch is ever made unless a test installs one.
pub fn handler<S: crate::results::ResultStore>(
    store: S,
    notifier: Arc<dyn Notifier>,
    vars: &[(&str, &str)],
    clock: &ManualClock,
) -> ResultHandler<S> {
    let (env, sink) = env(vars);
    ResultHandler::new(store, notifier, env, Arc::new(WarnOnce::new(sink)))
        .with_clock(clock.clock())
        .with_suppression_fetch(Arc::new(|_| Box::pin(async { None })))
}
