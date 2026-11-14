//! Schema contract verification and `--check-schema` (PLAN.md 2.2, 2.4).

mod common;

use common::{AGENT_TABLES_JSON, SCHEMA_SQL, TestDb};
use nyxmon_core::pyjson::loads;
use nyxmon_store::{MIN_NYXBOARD_MIGRATION, REQUIRED_COLUMNS, check_schema};

#[test]
fn contract_constants_match_the_generated_agent_tables() {
    let contract = loads(AGENT_TABLES_JSON).unwrap();
    let min = contract
        .get("min_nyxboard_migration")
        .unwrap()
        .as_list()
        .unwrap();
    assert_eq!(
        (min[0].as_str().unwrap(), min[1].as_str().unwrap()),
        MIN_NYXBOARD_MIGRATION
    );
    let tables = contract.get("tables").unwrap().as_dict().unwrap();
    assert_eq!(tables.len(), REQUIRED_COLUMNS.len());
    for (table, required) in REQUIRED_COLUMNS {
        let columns: Vec<&str> = tables[table]
            .as_list()
            .unwrap()
            .iter()
            .map(|column| column.as_str().unwrap())
            .collect();
        for column in required {
            assert!(
                columns.contains(column),
                "{table}.{column} is not in the Django schema"
            );
        }
    }
}

#[test]
fn schema_fixture_matches_the_generated_agent_tables() {
    let db = TestDb::new();
    let connection = db.raw();
    let contract = loads(AGENT_TABLES_JSON).unwrap();
    for (table, columns) in contract.get("tables").unwrap().as_dict().unwrap() {
        let mut statement = connection
            .prepare("SELECT name FROM pragma_table_info(?)")
            .unwrap();
        let present: Vec<String> = statement
            .query_map([table], |row| row.get(0))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
        let expected: Vec<&str> = columns
            .as_list()
            .unwrap()
            .iter()
            .map(|c| c.as_str().unwrap())
            .collect();
        assert_eq!(present, expected, "{table}");
    }
}

#[tokio::test]
async fn migrated_schema_passes() {
    let db = TestDb::new();

    let report = check_schema(db.path()).unwrap();
    assert!(report.is_ok(), "{report}");
    assert_eq!(report.error_message(), None);
    assert_eq!(report.journal_mode, "delete");
    assert_eq!(db.store().verify_schema().await.unwrap(), report);
}

#[test]
fn deliberately_old_schema_is_reported_precisely() {
    // The 0012-era database: no site-connectivity columns, no incident table,
    // migrations recorded only up to 0012.
    let schema = SCHEMA_SQL
        .replace(
            ", held_since INTEGER NOT NULL DEFAULT 0, attempt_seq INTEGER NOT NULL DEFAULT 0, \
             attempt_at INTEGER NOT NULL DEFAULT 0",
            "",
        )
        .replace(
            "CREATE TABLE collector_incident",
            "CREATE TABLE unrelated_incident",
        )
        .replace("'0013_site_connectivity_state'", "'0013_unrelated'");
    assert_ne!(schema, SCHEMA_SQL);
    let db = TestDb::with_schema(&schema);

    let report = check_schema(db.path()).unwrap();

    assert!(!report.is_ok());
    assert_eq!(report.missing_tables, ["collector_incident"]);
    let columns: Vec<String> = report
        .missing_columns
        .iter()
        .map(|(table, column)| format!("{table}.{column}"))
        .collect();
    assert_eq!(
        columns,
        [
            "check_notification_state.held_since",
            "check_notification_state.attempt_seq",
            "check_notification_state.attempt_at"
        ]
    );
    assert!(report.missing_migration);
    assert!(!report.missing_migrations_table);
    assert_eq!(
        report.error_message().unwrap(),
        "database schema is older than the agent contract; missing tables: collector_incident; \
         missing columns: check_notification_state.held_since, check_notification_state.attempt_seq, \
         check_notification_state.attempt_at; missing migration: nyxboard.0013_site_connectivity_state; \
         run `manage.py migrate` before starting the agent"
    );
}

#[test]
fn agent_only_database_without_migrations_is_rejected() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("db.sqlite3");
    rusqlite::Connection::open(&path)
        .unwrap()
        .execute_batch("CREATE TABLE health_check (id INTEGER PRIMARY KEY)")
        .unwrap();

    let report = check_schema(&path).unwrap();

    assert_eq!(
        report.missing_tables,
        [
            "check_result",
            "check_notification_state",
            "collector_incident"
        ]
    );
    assert_eq!(report.missing_columns.len(), 10);
    assert!(report.missing_migrations_table && report.missing_migration);
    assert!(
        report
            .error_message()
            .unwrap()
            .contains("missing migration: nyxboard.0013_site_connectivity_state (table django_migrations does not exist)")
    );
}

#[test]
fn check_schema_opens_read_only_and_never_creates() {
    let db = TestDb::new();
    let before = std::fs::read(db.path()).unwrap();
    check_schema(db.path()).unwrap();
    assert_eq!(std::fs::read(db.path()).unwrap(), before);

    let missing = db.path().with_file_name("missing.sqlite3");
    assert!(check_schema(&missing).is_err());
    assert!(!missing.exists());
}

#[test]
fn restored_schemas_must_retain_identity_and_relationship_constraints() {
    for (from, to) in [
        (
            "CREATE TABLE \"service\" (\"id\" integer NOT NULL PRIMARY KEY AUTOINCREMENT",
            "CREATE TABLE \"service\" (\"id\" integer NOT NULL",
        ),
        (
            "CREATE TABLE \"health_check\" (\"id\" integer NOT NULL PRIMARY KEY AUTOINCREMENT",
            "CREATE TABLE \"health_check\" (\"id\" integer NOT NULL",
        ),
        (
            "CREATE TABLE \"check_result\" (\"id\" integer NOT NULL PRIMARY KEY AUTOINCREMENT",
            "CREATE TABLE \"check_result\" (\"id\" integer NOT NULL",
        ),
        ("check_id INTEGER PRIMARY KEY", "check_id INTEGER"),
        ("incident_key  TEXT    PRIMARY KEY", "incident_key  TEXT"),
        ("REFERENCES \"health_check\" (\"id\")", ""),
        ("REFERENCES \"service\" (\"id\")", ""),
        (
            "REFERENCES health_check(id) ON DELETE CASCADE",
            "REFERENCES health_check(id)",
        ),
    ] {
        let schema = SCHEMA_SQL.replace(from, to);
        assert_ne!(schema, SCHEMA_SQL);
        // A deliberately broken parent key cannot accept fixture inserts with
        // FK enforcement on. Only the synthetic restore builder disables it.
        let db = TestDb::with_schema(&format!("PRAGMA foreign_keys = OFF;{schema}"));
        let before = std::fs::read(db.path()).unwrap();
        let report = check_schema(db.path()).unwrap();
        assert!(!report.is_ok(), "accepted missing {from}");
        assert!(report.error_message().unwrap().contains("constraint"));
        assert_eq!(std::fs::read(db.path()).unwrap(), before);
    }
}

#[test]
fn additive_columns_and_indexes_keep_the_contract() {
    let db = TestDb::new();
    db.raw().execute_batch("ALTER TABLE health_check ADD COLUMN extra TEXT; CREATE INDEX extra_idx ON health_check(extra)").unwrap();
    assert!(check_schema(db.path()).unwrap().is_ok());
}

#[test]
fn composite_or_partial_identity_keys_cannot_replace_required_keys() {
    let composite = SCHEMA_SQL
        .replace("incident_key  TEXT    PRIMARY KEY", "incident_key TEXT")
        .replace(
            "payload       TEXT    NOT NULL DEFAULT '{}'",
            "payload TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(incident_key, opened_at)",
        );
    let partial = format!(
        "{}; CREATE UNIQUE INDEX partial_identity ON check_notification_state(check_id) WHERE failure_count > 0;",
        SCHEMA_SQL.replace("check_id INTEGER PRIMARY KEY", "check_id INTEGER")
    );
    for schema in [composite, partial] {
        let db = TestDb::with_schema(&schema);
        assert!(!check_schema(db.path()).unwrap().is_ok());
    }
}
