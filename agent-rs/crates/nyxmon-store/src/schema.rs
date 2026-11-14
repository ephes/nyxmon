//! Schema contract verification (PLAN.md sections 2.2 and 2.4).
//!
//! The Rust agent runs no DDL. Before claiming anything it verifies that the
//! Django migrations created every table and column it uses and that the
//! minimum nyxboard migration is recorded. `nyxmon-agent --check-schema`
//! runs only this verification on a read-only connection ([`check_schema`]).
//!
//! `agent-rs/fixtures/schema/agent_tables.json` (generated from a migrated
//! database) pins the same contract; the tests fail when these constants
//! drift from it.

use std::fmt;
use std::path::Path;

use rusqlite::{Connection, OptionalExtension};

use crate::actor::{Store, open_read_only};
use crate::error::StoreError;

/// The minimum Django migration the agent needs, `(app, name)`.
pub const MIN_NYXBOARD_MIGRATION: (&str, &str) = ("nyxboard", "0013_site_connectivity_state");

/// Every table and column the agent reads or writes.
pub const REQUIRED_COLUMNS: [(&str, &[&str]); 4] = [
    (
        "health_check",
        &[
            "id",
            "service_id",
            "name",
            "check_type",
            "url",
            "check_interval",
            "next_check_time",
            "processing_started_at",
            "status",
            "disabled",
            "data",
        ],
    ),
    (
        "check_result",
        &["id", "health_check_id", "status", "data", "created_at"],
    ),
    (
        "check_notification_state",
        &[
            "check_id",
            "failure_count",
            "last_attempt_count",
            "last_immediate_at",
            "last_notified_at",
            "first_failure_at",
            "held_since",
            "attempt_seq",
            "attempt_at",
        ],
    ),
    (
        "collector_incident",
        &[
            "incident_key",
            "opened_at",
            "last_alert_at",
            "alert_count",
            "payload",
        ],
    ),
];

/// Outcome of the schema verification.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SchemaReport {
    /// Required tables that do not exist.
    pub missing_tables: Vec<String>,
    /// Required `(table, column)` pairs missing from existing tables.
    pub missing_columns: Vec<(String, String)>,
    /// Required identity or relationship constraints absent from restored tables.
    pub missing_constraints: Vec<String>,
    /// Whether the `django_migrations` table is missing.
    pub missing_migrations_table: bool,
    /// Whether [`MIN_NYXBOARD_MIGRATION`] is not recorded.
    pub missing_migration: bool,
    /// `PRAGMA journal_mode` (logged at INFO; never changed by the agent).
    pub journal_mode: String,
}

impl SchemaReport {
    /// Whether the database satisfies the contract.
    pub fn is_ok(&self) -> bool {
        self.missing_tables.is_empty()
            && self.missing_columns.is_empty()
            && self.missing_constraints.is_empty()
            && !self.missing_migration
    }

    /// The single error line naming every problem and the remedy, `None`
    /// when the schema is fine.
    pub fn error_message(&self) -> Option<String> {
        (!self.is_ok()).then(|| self.to_string())
    }
}

impl fmt::Display for SchemaReport {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.is_ok() {
            return write!(
                f,
                "database schema satisfies the agent contract (journal_mode={})",
                self.journal_mode
            );
        }
        let mut problems = Vec::new();
        if !self.missing_tables.is_empty() {
            problems.push(format!(
                "missing tables: {}",
                self.missing_tables.join(", ")
            ));
        }
        if !self.missing_columns.is_empty() {
            let columns: Vec<String> = self
                .missing_columns
                .iter()
                .map(|(table, column)| format!("{table}.{column}"))
                .collect();
            problems.push(format!("missing columns: {}", columns.join(", ")));
        }
        if !self.missing_constraints.is_empty() {
            problems.push(format!(
                "missing constraints: {}",
                self.missing_constraints.join(", ")
            ));
        }
        if self.missing_migration {
            let (app, name) = MIN_NYXBOARD_MIGRATION;
            let detail = if self.missing_migrations_table {
                " (table django_migrations does not exist)"
            } else {
                ""
            };
            problems.push(format!("missing migration: {app}.{name}{detail}"));
        }
        write!(
            f,
            "database schema is older than the agent contract; {}; run `manage.py migrate` \
             before starting the agent",
            problems.join("; ")
        )
    }
}

fn table_columns(connection: &Connection, table: &str) -> Result<Vec<String>, StoreError> {
    let mut statement = connection.prepare("SELECT name FROM pragma_table_info(?)")?;
    let rows = statement.query_map([table], |row| row.get::<_, String>(0))?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

// Exact single-column primary keys also provide the UNIQUE identity used by
// result IDs and ON CONFLICT notification/incident writes. Extra composite or
// partial unique indexes cannot substitute for these keys.
fn missing_constraints(connection: &Connection) -> Result<Vec<String>, StoreError> {
    let mut missing = Vec::new();
    for (table, column) in [
        ("service", "id"),
        ("health_check", "id"),
        ("check_result", "id"),
        ("check_notification_state", "check_id"),
        ("collector_incident", "incident_key"),
    ] {
        // Tables/columns have their own diagnostics.
        if table != "service" && table_columns(connection, table)?.is_empty() {
            continue;
        }
        let mut statement =
            connection.prepare("SELECT name FROM pragma_table_info(?) WHERE pk > 0 ORDER BY pk")?;
        let keys = statement
            .query_map([table], |row| row.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        if keys != [column] {
            missing.push(format!("{table} PRIMARY KEY ({column})"));
        }
    }
    for (table, column, parent, target, on_delete) in [
        ("health_check", "service_id", "service", "id", "NO ACTION"),
        (
            "check_result",
            "health_check_id",
            "health_check",
            "id",
            "NO ACTION",
        ),
        (
            "check_notification_state",
            "check_id",
            "health_check",
            "id",
            "CASCADE",
        ),
    ] {
        if table_columns(connection, table)?.is_empty() {
            continue;
        }
        let present: bool = connection.query_row(
            r#"SELECT EXISTS(
                SELECT 1 FROM pragma_foreign_key_list(?1) AS fk
                WHERE fk."from" = ?2 AND fk."table" = ?3 AND fk."to" = ?4
                  AND fk.on_delete = ?5
                  AND (SELECT COUNT(*) FROM pragma_foreign_key_list(?1) AS other WHERE other.id = fk.id) = 1
            )"#,
            [table, column, parent, target, on_delete], |row| row.get(0)
        )?;
        if !present {
            missing.push(format!(
                "{table}.{column} REFERENCES {parent}({target}) ON DELETE {on_delete}"
            ));
        }
    }
    Ok(missing)
}

/// Verify the contract on `connection` (read-only: no DDL, no pragma writes).
pub(crate) fn verify_schema(connection: &Connection) -> Result<SchemaReport, StoreError> {
    let mut missing_tables = Vec::new();
    let mut missing_columns = Vec::new();
    for (table, required) in REQUIRED_COLUMNS {
        // Every table has at least one column, so no rows = no table.
        let present = table_columns(connection, table)?;
        if present.is_empty() {
            missing_tables.push(table.to_owned());
            continue;
        }
        for column in required {
            if !present.iter().any(|name| name == column) {
                missing_columns.push((table.to_owned(), (*column).to_owned()));
            }
        }
    }
    let missing_constraints = missing_constraints(connection)?;
    let missing_migrations_table = table_columns(connection, "django_migrations")?.is_empty();
    let missing_migration = missing_migrations_table || {
        let (app, name) = MIN_NYXBOARD_MIGRATION;
        connection
            .query_row(
                "SELECT 1 FROM django_migrations WHERE app = ? AND name = ?",
                [app, name],
                |_| Ok(()),
            )
            .optional()?
            .is_none()
    };
    let journal_mode: String = connection.query_row("PRAGMA journal_mode", [], |row| row.get(0))?;
    Ok(SchemaReport {
        missing_tables,
        missing_columns,
        missing_constraints,
        missing_migrations_table,
        missing_migration,
        journal_mode,
    })
}

/// `--check-schema`: open `path` read-only and verify the contract.
///
/// # Errors
/// The database cannot be opened or queried.
pub fn check_schema(path: &Path) -> Result<SchemaReport, StoreError> {
    let connection = open_read_only(path)?;
    verify_schema(&connection)
}

impl Store {
    /// Verify the contract on the agent's connection (startup, before any
    /// claim).
    pub async fn verify_schema(&self) -> Result<SchemaReport, StoreError> {
        self.call(|connection| verify_schema(connection)).await
    }
}
