//! Test databases built from the Django schema fixture (PLAN.md 2.4, 7.1).

#![allow(dead_code)]

use std::path::{Path, PathBuf};

use nyxmon_core::PyJson;
use nyxmon_core::model::{Check, CheckResult, NotificationState, ResultStatus};
use nyxmon_core::pyjson::PyDict;
use nyxmon_store::Store;
use rusqlite::{Connection, OptionalExtension, params};

pub const SCHEMA_SQL: &str = include_str!("../../../../fixtures/schema/schema.sql");
pub const AGENT_TABLES_JSON: &str = include_str!("../../../../fixtures/schema/agent_tables.json");

/// A temporary database migrated to the committed Django schema.
pub struct TestDb {
    _dir: tempfile::TempDir,
    pub path: PathBuf,
}

impl TestDb {
    pub fn new() -> TestDb {
        TestDb::with_schema(SCHEMA_SQL)
    }

    pub fn with_schema(schema: &str) -> TestDb {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("db.sqlite3");
        let connection = Connection::open(&path).unwrap();
        connection.execute_batch(schema).unwrap();
        connection
            .execute("INSERT INTO service (id, name) VALUES (1, 'service')", [])
            .unwrap();
        TestDb { _dir: dir, path }
    }

    pub fn store(&self) -> Store {
        Store::open(&self.path).unwrap()
    }

    /// A second connection, like Django's (foreign keys on, as Django sets
    /// them).
    pub fn raw(&self) -> Connection {
        let connection = Connection::open(&self.path).unwrap();
        connection
            .execute_batch("PRAGMA foreign_keys = ON")
            .unwrap();
        connection
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Insert a check row (Django's columns; defaults like `_check` in the
    /// Python tests).
    pub fn insert(&self, row: CheckRow) {
        self.raw()
            .execute(
                "INSERT INTO health_check (id, service_id, name, check_type, url, check_interval,
                    next_check_time, processing_started_at, status, disabled, data)
                 VALUES (?, 1, ?, 'http', ?, ?, ?, ?, ?, ?, ?)",
                params![
                    row.id,
                    format!("check {}", row.id),
                    format!("https://example.test/{}", row.id),
                    row.check_interval,
                    row.next_check_time,
                    row.processing_started_at,
                    row.status,
                    row.disabled,
                    row.data,
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

    pub fn state_row(&self, check_id: i64) -> Option<[i64; 8]> {
        self.raw()
            .query_row(
                "SELECT failure_count, last_attempt_count, last_immediate_at, last_notified_at,
                    first_failure_at, held_since, attempt_seq, attempt_at
                 FROM check_notification_state WHERE check_id = ?",
                [check_id],
                |row| {
                    let mut values = [0; 8];
                    for (index, value) in values.iter_mut().enumerate() {
                        *value = row.get(index)?;
                    }
                    Ok(values)
                },
            )
            .optional()
            .unwrap()
    }

    /// `(status, next_check_time, processing_started_at)` of a check.
    pub fn schedule(&self, check_id: i64) -> Option<(String, i64, i64)> {
        self.raw()
            .query_row(
                "SELECT status, next_check_time, processing_started_at FROM health_check
                 WHERE id = ?",
                [check_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()
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

    /// Delete a check like Django does (foreign keys on, results first).
    pub fn delete_check(&self, check_id: i64) {
        let connection = self.raw();
        connection
            .execute(
                "DELETE FROM check_result WHERE health_check_id = ?",
                [check_id],
            )
            .unwrap();
        connection
            .execute("DELETE FROM health_check WHERE id = ?", [check_id])
            .unwrap();
    }
}

/// A `health_check` row for [`TestDb::insert`].
#[derive(Clone, Debug)]
pub struct CheckRow {
    pub id: i64,
    pub check_interval: i64,
    pub next_check_time: i64,
    pub processing_started_at: i64,
    pub status: &'static str,
    pub disabled: bool,
    pub data: String,
}

pub fn row(id: i64) -> CheckRow {
    CheckRow {
        id,
        check_interval: 300,
        next_check_time: 0,
        processing_started_at: 0,
        status: "idle",
        disabled: false,
        data: "{}".to_owned(),
    }
}

impl CheckRow {
    pub fn next(mut self, next_check_time: i64) -> Self {
        self.next_check_time = next_check_time;
        self
    }

    pub fn processing(mut self, started_at: i64) -> Self {
        self.status = "processing";
        self.processing_started_at = started_at;
        self
    }

    pub fn started(mut self, started_at: i64) -> Self {
        self.processing_started_at = started_at;
        self
    }

    pub fn disabled(mut self) -> Self {
        self.disabled = true;
        self
    }

    pub fn data(mut self, data: &str) -> Self {
        self.data = data.to_owned();
        self
    }
}

/// An idle in-memory check as `_check(id)` builds it.
pub fn check(id: i64) -> Check {
    Check {
        check_id: id,
        service_id: 1,
        name: format!("check {id}"),
        check_type: "http".to_owned(),
        url: format!("https://example.test/{id}"),
        check_interval: 300,
        next_check_time: 0,
        processing_started_at: 0,
        status: "idle".to_owned(),
        disabled: false,
        data: PyJson::dict(),
        claim_started_at: 0,
    }
}

pub fn result(check_id: i64, status: ResultStatus) -> CheckResult {
    CheckResult::new(check_id, status, PyDict::new())
}

pub fn error(check_id: i64) -> CheckResult {
    result(check_id, ResultStatus::Error)
}

pub fn state(values: [i64; 8]) -> NotificationState {
    let row: Vec<Option<i64>> = values.iter().copied().map(Some).collect();
    NotificationState::from_row(Some(&row))
}

pub fn dict(json: &str) -> PyDict {
    nyxmon_core::pyjson::loads(json)
        .unwrap()
        .into_dict()
        .unwrap()
}

/// `json.dumps` text of a payload (checks key order, unlike `PyDict ==`).
pub fn dumps(payload: &PyDict) -> String {
    PyJson::Dict(payload.clone()).to_json_string().unwrap()
}
