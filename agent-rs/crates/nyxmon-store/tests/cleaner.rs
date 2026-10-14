//! Result retention (`tests/unit/repositories/sqlite_repo_test.py`).

mod common;

use common::{SCHEMA_SQL, TestDb, row};
use rusqlite::params;

/// Insert a result created `age_seconds` ago (SQLite UTC clock).
fn insert_result(db: &TestDb, id: i64, age_seconds: i64) {
    db.raw()
        .execute(
            "INSERT INTO check_result (id, health_check_id, status, data, created_at)
             VALUES (?, 1, 'ok', '{}', datetime('now', ?))",
            params![id, format!("-{age_seconds} seconds")],
        )
        .unwrap();
}

fn result_ids(db: &TestDb) -> Vec<i64> {
    let connection = db.raw();
    let mut statement = connection
        .prepare("SELECT id FROM check_result ORDER BY id")
        .unwrap();
    statement
        .query_map([], |row| row.get(0))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap()
}

fn populated() -> TestDb {
    let db = TestDb::new();
    db.insert(row(1));
    insert_result(&db, 1, 10 * 60);
    insert_result(&db, 2, 25 * 3600);
    insert_result(&db, 3, 48 * 3600);
    db
}

#[tokio::test]
async fn deletes_results_older_than_the_retention() {
    let db = populated();
    let store = db.store();

    assert_eq!(store.delete_old_results(86_400, 1000).await.unwrap(), 2);
    assert_eq!(result_ids(&db), [1]);
    assert_eq!(store.delete_old_results(86_400, 1000).await.unwrap(), 0);
}

#[tokio::test]
async fn custom_retention() {
    let db = populated();
    let store = db.store();

    assert_eq!(store.delete_old_results(30 * 3600, 1000).await.unwrap(), 1);
    assert_eq!(result_ids(&db), [1, 2]);
}

#[tokio::test]
async fn batches_are_bounded_and_ordered_by_id() {
    let db = populated();
    let store = db.store();

    assert_eq!(store.delete_old_results(86_400, 1).await.unwrap(), 1);
    assert_eq!(result_ids(&db), [1, 3]);
    assert_eq!(store.delete_old_results(86_400, 1).await.unwrap(), 1);
    assert_eq!(store.delete_old_results(86_400, 1).await.unwrap(), 0);
    assert_eq!(result_ids(&db), [1]);
}

#[tokio::test]
async fn expired_sessions_are_deleted_oldest_first_and_bounded() {
    let db = populated();
    let connection = db.raw();
    for (key, offset) in [("old", "-2 days"), ("older", "-3 days"), ("live", "+1 day")] {
        connection
            .execute(
                "INSERT INTO django_session (session_key, session_data, expire_date)
                 VALUES (?, '', datetime('now', ?))",
                [key, offset],
            )
            .unwrap();
    }
    let store = db.store();

    // The session batch does not count towards the returned result count.
    assert_eq!(store.delete_old_results(86_400, 1).await.unwrap(), 1);
    let sessions = |db: &TestDb| -> Vec<String> {
        let connection = db.raw();
        let mut statement = connection
            .prepare("SELECT session_key FROM django_session ORDER BY session_key")
            .unwrap();
        statement
            .query_map([], |row| row.get(0))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap()
    };
    assert_eq!(sessions(&db), ["live", "old"]);
    store.delete_old_results(86_400, 1).await.unwrap();
    assert_eq!(sessions(&db), ["live"]);
}

#[tokio::test]
async fn databases_without_the_sessions_table_are_supported() {
    let schema = SCHEMA_SQL.replace(
        r#"CREATE TABLE "django_session""#,
        r#"CREATE TABLE "not_django_session""#,
    );
    let schema = schema.replace(r#"ON "django_session""#, r#"ON "not_django_session""#);
    let db = TestDb::with_schema(&schema);
    db.insert(row(1));
    insert_result(&db, 1, 48 * 3600);

    assert_eq!(
        db.store().delete_old_results(86_400, 1000).await.unwrap(),
        1
    );
}
