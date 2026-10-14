//! Claims, stale reclaim, release, holds and notification-state reads
//! (`tests/adapters/test_sqlite_repo.py`, `test_site_connectivity_persistence.py`).

mod common;

use common::{TestDb, row, state};
use nyxmon_core::PyJson;
use nyxmon_core::model::{CHECK_STATUS_IDLE, CHECK_STATUS_PROCESSING, NotificationState};
use nyxmon_store::data_is_undecodable;

/// Claimed ids, sorted: `RETURNING` yields rows in SQLite's update order,
/// which the Python tests do not pin either (they compare sets).
fn ids(checks: &[nyxmon_core::model::Check]) -> Vec<i64> {
    let mut ids: Vec<i64> = checks.iter().map(|check| check.check_id).collect();
    ids.sort_unstable();
    ids
}

#[tokio::test]
async fn claim_returns_every_column_and_stamps_the_claim() {
    let db = TestDb::new();
    db.insert(row(1).data(r#"{"b": 1, "a": [1.5, "x"], "nested": {"k": null}}"#));
    let store = db.store();

    let [claimed] = store
        .claim_due_checks(10_000, 5)
        .await
        .unwrap()
        .try_into()
        .unwrap();

    assert_eq!(claimed.check_id, 1);
    assert_eq!(claimed.service_id, 1);
    assert_eq!(claimed.name, "check 1");
    assert_eq!(claimed.check_type, "http");
    assert_eq!(claimed.url, "https://example.test/1");
    assert_eq!(claimed.check_interval, 300);
    assert_eq!(claimed.status, CHECK_STATUS_PROCESSING);
    assert_eq!(claimed.processing_started_at, 10_000);
    assert_eq!(claimed.claim_started_at, 10_000);
    assert!(!claimed.disabled);
    assert_eq!(
        claimed.data.to_json_string().unwrap(),
        r#"{"b": 1, "a": [1.5, "x"], "nested": {"k": null}}"#
    );
    assert_eq!(db.schedule(1), Some(("processing".to_owned(), 0, 10_000)));
    assert!(store.claim_due_checks(10_000, 5).await.unwrap().is_empty());
}

#[tokio::test]
async fn due_check_claims_are_bounded_and_ordered() {
    let db = TestDb::new();
    for id in 1..=7 {
        db.insert(row(id).next(100 - id));
    }
    // Not due, disabled, or already processing: never claimed.
    db.insert(row(8).next(10_001));
    db.insert(row(9).disabled());
    db.insert(row(10).processing(50));
    let store = db.store();

    let first = store.claim_due_checks(10_000, 5).await.unwrap();
    let second = store.claim_due_checks(10_000, 5).await.unwrap();

    assert_eq!(ids(&first), [3, 4, 5, 6, 7]);
    assert_eq!(ids(&second), [1, 2]);
    assert!(store.claim_due_checks(10_000, 5).await.unwrap().is_empty());
}

#[tokio::test]
async fn data_column_decoding() {
    let db = TestDb::new();
    db.insert(row(1).data("{}"));
    db.insert(row(2).data("null"));
    db.insert(row(3).data("[1, 2]"));
    db.insert(row(4).data(r#"{"x": 1e999}"#));
    // JSON_VALID accepts a lone surrogate escape; Rust cannot hold it (D-8).
    db.insert(row(5).data(r#"{"x": "\ud800"}"#));
    db.insert(row(6));
    db.raw()
        .execute_batch(
            "PRAGMA ignore_check_constraints = ON;
             UPDATE health_check SET data = '' WHERE id = 1;
             UPDATE health_check SET data = 'not json' WHERE id = 6;",
        )
        .unwrap();

    let checks = db.store().list_checks().await.unwrap();
    let data: Vec<&PyJson> = checks.iter().map(|check| &check.data).collect();

    assert_eq!(data[0], &PyJson::dict(), "empty text reads as {{}}");
    assert_eq!(data[1], &PyJson::dict(), "null reads as {{}} (D-11)");
    assert_eq!(data[2].to_json_string().unwrap(), "[1, 2]");
    assert_eq!(data[3].to_json_string().unwrap(), r#"{"x": Infinity}"#);
    assert!(data_is_undecodable(data[4]), "lone surrogate (D-8)");
    assert!(data_is_undecodable(data[5]), "invalid JSON");
    assert!(!data_is_undecodable(data[0]));
}

#[tokio::test]
async fn reclaims_only_expired_processing_leases() {
    let now = 10_000;
    let db = TestDb::new();
    db.insert(row(1).processing(now - 601));
    db.insert(row(2).processing(now - 10));
    db.insert(row(3).processing(0));
    let store = db.store();

    let reclaimed = store.reclaim_stale_checks(now, 300, 5).await.unwrap();

    assert_eq!(ids(&reclaimed), [1]);
    assert_eq!(reclaimed[0].status, CHECK_STATUS_IDLE);
    assert_eq!(reclaimed[0].processing_started_at, 0);
    assert_eq!(reclaimed[0].claim_started_at, 0);
    assert_eq!(db.schedule(1), Some(("idle".to_owned(), 0, 0)));
    assert_eq!(db.schedule(2), Some(("processing".to_owned(), 0, now - 10)));
    // A legacy claim without a timestamp gets a full lease from now on.
    assert_eq!(db.schedule(3), Some(("processing".to_owned(), 0, now)));
    assert_eq!(
        ids(&store.reclaim_stale_checks(now + 299, 300, 5).await.unwrap()),
        [2]
    );
    assert_eq!(
        ids(&store.reclaim_stale_checks(now + 300, 300, 5).await.unwrap()),
        [3]
    );
}

#[tokio::test]
async fn reclaim_without_candidates_writes_nothing() {
    let db = TestDb::new();
    db.insert(row(1).processing(9_990));
    db.insert(row(2).started(5));
    let store = db.store();

    assert!(
        store
            .reclaim_stale_checks(10_000, 300, 5)
            .await
            .unwrap()
            .is_empty()
    );
    assert_eq!(db.schedule(1), Some(("processing".to_owned(), 0, 9_990)));
    assert_eq!(db.schedule(2), Some(("idle".to_owned(), 0, 5)));
}

#[tokio::test]
async fn claim_completion_and_expiry_lifecycle() {
    let db = TestDb::new();
    db.insert(row(1));
    let store = db.store();
    let mut now = 10_000;

    let claimed = store.claim_due_checks(now, 5).await.unwrap();
    assert_eq!(claimed[0].processing_started_at, now);
    assert!(
        store
            .reclaim_stale_checks(now, 300, 5)
            .await
            .unwrap()
            .is_empty()
    );

    now += 301;
    let reclaimed = store.reclaim_stale_checks(now, 300, 5).await.unwrap();
    assert_eq!(ids(&reclaimed), [1]);
    assert_eq!(reclaimed[0].status, CHECK_STATUS_IDLE);
    // The reclaimed row keeps its next_check_time, so it is due again.
    assert_eq!(ids(&store.claim_due_checks(now, 5).await.unwrap()), [1]);
}

#[tokio::test]
async fn stale_reclaims_are_oldest_first_and_bounded() {
    let db = TestDb::new();
    for id in 1..=7 {
        db.insert(row(id).processing(100 - id));
    }
    let store = db.store();

    let first = store.reclaim_stale_checks(1_000, 300, 5).await.unwrap();
    let second = store.reclaim_stale_checks(1_000, 300, 5).await.unwrap();

    assert_eq!(ids(&first), [3, 4, 5, 6, 7]);
    assert_eq!(ids(&second), [1, 2]);
}

#[tokio::test]
async fn release_returns_only_this_process_claims() {
    let db = TestDb::new();
    for id in 1..=4 {
        db.insert(row(id));
    }
    db.raw()
        .execute(
            "UPDATE health_check SET check_interval = 0 WHERE id = 2",
            [],
        )
        .unwrap();
    let store = db.store();
    let claims = store.claim_due_checks(1_000, 5).await.unwrap();
    // Check 3 was reclaimed and claimed again by another collector; check 4
    // completed meanwhile.
    db.raw()
        .execute(
            "UPDATE health_check SET processing_started_at = 1500 WHERE id = 3",
            [],
        )
        .unwrap();
    db.raw()
        .execute(
            "UPDATE health_check SET status = 'idle', processing_started_at = 0,
                next_check_time = 7000 WHERE id = 4",
            [],
        )
        .unwrap();

    assert_eq!(store.release_claims(2_000, &claims).await.unwrap(), 2);

    assert_eq!(db.schedule(1), Some(("idle".to_owned(), 2_300, 0)));
    // `MAX(1, check_interval)`.
    assert_eq!(db.schedule(2), Some(("idle".to_owned(), 2_001, 0)));
    assert_eq!(db.schedule(3), Some(("processing".to_owned(), 0, 1_500)));
    assert_eq!(db.schedule(4), Some(("idle".to_owned(), 7_000, 0)));
    assert!(db.results().is_empty());
    assert_eq!(db.state_row(1), None);
    assert_eq!(store.release_claims(2_000, &[]).await.unwrap(), 0);
}

#[tokio::test]
async fn release_ignores_claims_without_a_timestamp() {
    let db = TestDb::new();
    db.insert(row(1).processing(0));
    let store = db.store();
    let mut legacy = common::check(1);
    legacy.status = CHECK_STATUS_PROCESSING.to_owned();

    assert_eq!(store.release_claims(2_000, &[legacy]).await.unwrap(), 0);
    assert_eq!(db.schedule(1), Some(("processing".to_owned(), 0, 0)));
}

#[tokio::test]
async fn notification_state_reads_zeros_for_a_missing_row() {
    let db = TestDb::new();
    db.insert(row(7));
    let store = db.store();

    assert_eq!(
        store.get_notification_state(7).await.unwrap(),
        NotificationState::default()
    );
    let full = state([2, 2, 500, 900, 800, 700, 5, 950]);
    db.set_state(7, full);
    assert_eq!(store.get_notification_state(7).await.unwrap(), full);
}

#[tokio::test]
async fn acknowledgement_is_fenced_by_the_attempt_sequence() {
    let db = TestDb::new();
    db.insert(row(1));
    db.set_state(1, state([2, 2, 0, 900, 800, 0, 7, 950]));
    let store = db.store();

    assert!(!store.acknowledge_notification_attempt(1, 6).await.unwrap());
    assert_eq!(
        store.get_notification_state(1).await.unwrap().attempt_at,
        950
    );

    assert!(store.acknowledge_notification_attempt(1, 7).await.unwrap());
    assert_eq!(db.state_row(1), Some([2, 2, 0, 900, 800, 0, 7, 0]));

    assert!(!store.acknowledge_notification_attempt(1, 7).await.unwrap());
    assert!(!store.acknowledge_notification_attempt(99, 1).await.unwrap());
}

#[tokio::test]
async fn acknowledgement_after_an_ok_cleared_the_row_changes_nothing() {
    let db = TestDb::new();
    db.insert(row(1));
    let recovered = state([2, 2, 0, 900, 800, 0, 7, 950]).cleared();
    db.set_state(1, recovered);
    let store = db.store();

    assert!(!store.acknowledge_notification_attempt(1, 7).await.unwrap());
    assert_eq!(store.get_notification_state(1).await.unwrap(), recovered);
}

#[tokio::test]
async fn list_held_checks_joins_state_and_check() {
    let db = TestDb::new();
    db.insert(
        row(1)
            .next(5_000)
            .data(r#"{"site_dependency": "internet"}"#),
    );
    db.insert(row(2).next(6_000));
    db.insert(row(3).next(7_000).disabled());
    db.set_state(1, state([2, 0, 0, 0, 800, 900, 0, 0]));
    db.set_state(2, state([2, 0, 0, 0, 800, 0, 0, 0]));
    db.set_state(3, state([1, 0, 0, 0, 800, 950, 0, 0]));
    let store = db.store();

    let held = store.list_held_checks().await.unwrap();

    assert_eq!(held.len(), 2);
    assert_eq!(
        (
            held[0].check_id,
            held[0].status.as_str(),
            held[0].disabled,
            held[0].next_check_time,
            held[0].held_since
        ),
        (1, "idle", false, 5_000, 900)
    );
    assert_eq!(
        held[0].data.to_json_string().unwrap(),
        r#"{"site_dependency": "internet"}"#
    );
    assert_eq!(
        (
            held[1].check_id,
            held[1].disabled,
            held[1].next_check_time,
            held[1].held_since
        ),
        (3, true, 7_000, 950)
    );
    assert_eq!(held[1].data, PyJson::dict());
    assert_eq!(store.count_held_checks().await.unwrap(), 2);
}

#[tokio::test]
async fn reschedule_touches_only_idle_enabled_and_later_checks() {
    let db = TestDb::new();
    db.insert(row(1).next(9_000));
    db.insert(row(2).next(9_000).disabled());
    db.insert(row(3).next(9_000).processing(4_000));
    db.insert(row(4).next(4_000));
    let store = db.store();

    assert_eq!(
        store
            .reschedule_checks(&[1, 2, 3, 4, 99], 5_000)
            .await
            .unwrap(),
        1
    );
    assert_eq!(store.reschedule_checks(&[], 5_000).await.unwrap(), 0);

    let scheduled: Vec<i64> = (1..=4).map(|id| db.schedule(id).unwrap().1).collect();
    assert_eq!(scheduled, [5_000, 9_000, 9_000, 4_000]);
}

#[tokio::test]
async fn counts_only_processing_claims_older_than_the_epoch() {
    let db = TestDb::new();
    db.insert(row(1).processing(4_000));
    db.insert(row(2).processing(5_000));
    db.insert(row(3).processing(6_000));
    db.insert(row(4).started(1_000));
    db.insert(row(5).processing(0));
    let store = db.store();

    assert_eq!(
        store.count_processing_claims_before(5_000).await.unwrap(),
        1
    );
    assert_eq!(
        store.count_processing_claims_before(6_001).await.unwrap(),
        3
    );
    assert_eq!(store.count_processing_claims_before(0).await.unwrap(), 0);
}

#[test]
fn read_check_loads_one_row_read_only() {
    // `run-check` (PLAN.md 7.4): the row as a claim would load it, without
    // writing anything (the claim columns stay as they are).
    let db = TestDb::new();
    db.insert(row(1).processing(50).disabled().data(r#"{"b": 1, "a": 2}"#));
    db.insert(row(2));

    let check = nyxmon_store::read_check(db.path(), 1).unwrap().unwrap();
    assert_eq!(check.check_id, 1);
    assert_eq!(check.status, CHECK_STATUS_PROCESSING);
    assert_eq!(check.claim_started_at, 50);
    assert!(check.disabled);
    assert_eq!(check.data.to_json_string().unwrap(), r#"{"b": 1, "a": 2}"#);
    assert_eq!(db.schedule(1), Some(("processing".to_owned(), 0, 50)));
    assert!(nyxmon_store::read_check(db.path(), 3).unwrap().is_none());

    // A read-only connection cannot create a missing database.
    let missing = db.path().with_file_name("missing.sqlite3");
    assert!(nyxmon_store::read_check(&missing, 1).is_err());
    assert!(!missing.exists());
}
