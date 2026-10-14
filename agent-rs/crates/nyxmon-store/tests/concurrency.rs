//! Lock waits against another connection (PLAN.md 3.3, 7.1): every write
//! transaction starts with `BEGIN IMMEDIATE` and waits up to the busy
//! timeout for a writer such as Django or a backup.

mod common;

use std::time::{Duration, Instant};

use common::{TestDb, row};
use nyxmon_store::{Store, StoreOptions};

#[tokio::test]
async fn a_write_waits_for_another_writer_within_the_busy_timeout() {
    let db = TestDb::new();
    db.insert(row(1));
    let store = db.store();
    let blocker = db.raw();
    blocker.execute_batch("BEGIN IMMEDIATE").unwrap();

    let started = Instant::now();
    let claim = tokio::spawn({
        let store = store.clone();
        async move { store.claim_due_checks(10_000, 5).await }
    });
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(
        !claim.is_finished(),
        "the claim must wait for the write lock"
    );
    blocker.execute_batch("COMMIT").unwrap();

    let claimed = claim.await.unwrap().unwrap();
    assert_eq!(claimed.len(), 1);
    assert!(started.elapsed() >= Duration::from_millis(300));
}

#[tokio::test]
async fn a_write_fails_busy_after_the_timeout_and_changes_nothing() {
    let db = TestDb::new();
    db.insert(row(1));
    let store = Store::open_with(
        db.path(),
        StoreOptions {
            busy_timeout: Duration::from_millis(200),
        },
    )
    .unwrap();
    let blocker = db.raw();
    blocker.execute_batch("BEGIN IMMEDIATE").unwrap();

    let err = store.claim_due_checks(10_000, 5).await.unwrap_err();

    assert!(err.is_busy(), "{err:?}");
    blocker.execute_batch("ROLLBACK").unwrap();
    assert_eq!(db.schedule(1), Some(("idle".to_owned(), 0, 0)));
    // The actor survives a failed operation.
    assert_eq!(store.claim_due_checks(10_000, 5).await.unwrap().len(), 1);
}

#[tokio::test]
async fn reads_do_not_take_the_write_lock() {
    let db = TestDb::new();
    db.insert(row(1));
    let store = Store::open_with(
        db.path(),
        StoreOptions {
            busy_timeout: Duration::from_millis(200),
        },
    )
    .unwrap();
    let blocker = db.raw();
    blocker.execute_batch("BEGIN IMMEDIATE").unwrap();

    // A RESERVED lock still admits readers in rollback-journal mode.
    assert_eq!(store.list_checks().await.unwrap().len(), 1);
    assert_eq!(store.count_held_checks().await.unwrap(), 0);
    blocker.execute_batch("ROLLBACK").unwrap();
}
