//! `persist_check_result` (`tests/adapters/test_sqlite_repo.py`,
//! `test_site_connectivity_persistence.py`).

mod common;

use common::{TestDb, check, dict, error, result, row, state};
use nyxmon_core::PyJson;
use nyxmon_core::model::{Check, CheckResult, NotificationState, ResultStatus};
use nyxmon_store::{PersistRequest, StoreError};

fn request(
    check: &Check,
    result: CheckResult,
    transition: Option<(NotificationState, NotificationState)>,
) -> PersistRequest {
    PersistRequest {
        transition,
        ..PersistRequest::new(check.clone(), result)
    }
}

fn hold(check: &Check, hold_marker: i64) -> PersistRequest {
    PersistRequest {
        hold_marker: Some(hold_marker),
        ..PersistRequest::new(check.clone(), error(check.check_id))
    }
}

fn zero() -> NotificationState {
    NotificationState::default()
}

/// A claimed check after `schedule_next_check(now)`.
fn completed(claimed: &Check, now: i64) -> Check {
    let mut check = claimed.clone();
    check.schedule_next_check(now);
    check
}

#[tokio::test]
async fn result_row_matches_python_storage() {
    let db = TestDb::new();
    db.insert(row(1));
    let store = db.store();
    let data = dict(r#"{"error_type": "http_error", "error_msg": "café 😀", "x": 1.0}"#);

    assert!(
        store
            .persist_check_result(request(
                &check(1),
                CheckResult::new(1, ResultStatus::Warning, data),
                None
            ))
            .await
            .unwrap()
    );

    let (check_id, status, text, created_at): (i64, String, String, String) = db
        .raw()
        .query_row(
            "SELECT health_check_id, status, data, created_at FROM check_result",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .unwrap();
    assert_eq!((check_id, status.as_str()), (1, "warning"));
    // `json.dumps` defaults: ", "/": " separators, ensure_ascii, float repr.
    assert_eq!(
        text,
        r#"{"error_type": "http_error", "error_msg": "caf\u00e9 \ud83d\ude00", "x": 1.0}"#
    );
    // SQLite `datetime('now')`: UTC `YYYY-MM-DD HH:MM:SS`.
    assert_eq!(created_at.len(), 19);
    assert_eq!(&created_at[10..11], " ");
}

#[tokio::test]
async fn result_and_notification_state_are_atomic() {
    let db = TestDb::new();
    db.insert(row(1));
    let store = db.store();

    let applied = store
        .persist_check_result(request(
            &check(1),
            error(1),
            Some((zero(), state([2, 2, 0, 0, 0, 0, 0, 0]))),
        ))
        .await
        .unwrap();

    assert!(applied);
    assert_eq!(db.results().len(), 1);
    assert_eq!(db.state_row(1), Some([2, 2, 0, 0, 0, 0, 0, 0]));
}

#[tokio::test]
async fn all_eight_fields_round_trip_through_persist() {
    let db = TestDb::new();
    db.insert(row(1));
    let store = db.store();
    let full = state([2, 2, 500, 900, 800, 700, 5, 950]);

    assert!(
        store
            .persist_check_result(request(&check(1), error(1), Some((zero(), full))))
            .await
            .unwrap()
    );

    assert_eq!(store.get_notification_state(1).await.unwrap(), full);
    assert_eq!(db.state_row(1), Some([2, 2, 500, 900, 800, 700, 5, 950]));
}

#[tokio::test]
async fn unchanged_transition_writes_no_state_row() {
    let db = TestDb::new();
    db.insert(row(1));
    let store = db.store();

    assert!(
        store
            .persist_check_result(request(&check(1), error(1), Some((zero(), zero()))))
            .await
            .unwrap()
    );

    assert_eq!(db.state_row(1), None);
    assert_eq!(db.results().len(), 1);
}

#[tokio::test]
async fn state_write_failure_rolls_back_result_and_check() {
    let db = TestDb::new();
    db.insert(row(1).processing(4_000));
    db.raw()
        .execute_batch(
            "CREATE TRIGGER reject_notification_state BEFORE INSERT ON check_notification_state
             BEGIN SELECT RAISE(ABORT, 'state write rejected'); END",
        )
        .unwrap();
    let store = db.store();
    let mut claimed = check(1);
    claimed.claim_started_at = 4_000;

    let err = store
        .persist_check_result(request(
            &completed(&claimed, 9_000),
            error(1),
            Some((zero(), state([1, 1, 0, 0, 0, 0, 0, 0]))),
        ))
        .await
        .unwrap_err();

    assert!(err.to_string().contains("state write rejected"), "{err}");
    assert_eq!(db.schedule(1), Some(("processing".to_owned(), 0, 4_000)));
    assert!(db.results().is_empty());
}

#[tokio::test]
async fn stale_notification_state_is_rejected_atomically() {
    let db = TestDb::new();
    db.insert(row(1).processing(4_000));
    db.set_state(1, state([1, 0, 0, 0, 0, 0, 0, 0]));
    let store = db.store();
    let mut claimed = check(1);
    claimed.claim_started_at = 4_000;

    let err = store
        .persist_check_result(request(
            &completed(&claimed, 9_000),
            error(1),
            Some((zero(), state([1, 1, 0, 0, 0, 0, 0, 0]))),
        ))
        .await
        .unwrap_err();

    assert!(matches!(
        err,
        StoreError::NotificationStateConflict { check_id: 1 }
    ));
    assert_eq!(db.schedule(1), Some(("processing".to_owned(), 0, 4_000)));
    assert!(db.results().is_empty());
    assert_eq!(db.state_row(1), Some([1, 0, 0, 0, 0, 0, 0, 0]));
}

#[tokio::test]
async fn conflict_is_detected_on_reminder_timestamps_alone() {
    let db = TestDb::new();
    db.insert(row(1));
    db.set_state(1, state([3, 3, 0, 5_000, 4_000, 0, 0, 0]));
    let store = db.store();
    let stale = state([3, 3, 0, 1_000, 4_000, 0, 0, 0]);

    let err = store
        .persist_check_result(PersistRequest {
            complete_check: false,
            ..request(
                &check(1),
                error(1),
                Some((stale, state([4, 4, 0, 9_000, 4_000, 0, 0, 0]))),
            )
        })
        .await
        .unwrap_err();

    assert!(matches!(err, StoreError::NotificationStateConflict { .. }));
    assert_eq!(db.state_row(1), Some([3, 3, 0, 5_000, 4_000, 0, 0, 0]));
    assert!(db.results().is_empty());
}

#[tokio::test]
async fn stale_expectation_on_a_new_field_still_conflicts() {
    let db = TestDb::new();
    db.insert(row(1));
    db.set_state(1, state([3, 3, 0, 900, 800, 700, 4, 950]));
    let store = db.store();

    let err = store
        .persist_check_result(PersistRequest {
            complete_check: false,
            ..request(
                &check(1),
                error(1),
                Some((
                    state([3, 3, 0, 900, 800, 700, 3, 0]),
                    state([4, 4, 0, 900, 800, 700, 4, 1_000]),
                )),
            )
        })
        .await
        .unwrap_err();

    assert!(matches!(err, StoreError::NotificationStateConflict { .. }));
    assert_eq!(db.state_row(1), Some([3, 3, 0, 900, 800, 700, 4, 950]));
}

#[tokio::test]
async fn late_completion_cannot_release_newer_claim() {
    let db = TestDb::new();
    db.insert(row(1));
    let store = db.store();
    let mut now = 10_000;
    let [first_claim] = store
        .claim_due_checks(now, 5)
        .await
        .unwrap()
        .try_into()
        .unwrap();
    now += 301;
    store.reclaim_stale_checks(now, 300, 5).await.unwrap();
    now += 1;
    let [second_claim] = store
        .claim_due_checks(now, 5)
        .await
        .unwrap()
        .try_into()
        .unwrap();
    db.set_state(1, state([4, 2, 123, 0, 0, 0, 0, 0]));

    let applied = store
        .persist_check_result(request(
            &completed(&first_claim, now),
            result(1, ResultStatus::Ok),
            Some((state([4, 2, 123, 0, 0, 0, 0, 0]), zero())),
        ))
        .await
        .unwrap();

    assert!(!applied);
    assert_eq!(
        db.schedule(1),
        Some((
            "processing".to_owned(),
            0,
            second_claim.processing_started_at
        ))
    );
    assert_eq!(db.state_row(1), Some([4, 2, 123, 0, 0, 0, 0, 0]));
    assert_eq!(db.results().len(), 1);
}

#[tokio::test]
async fn late_completion_cannot_override_reclaimed_idle_backoff() {
    let db = TestDb::new();
    db.insert(row(1));
    let store = db.store();
    let [original] = store
        .claim_due_checks(10_000, 5)
        .await
        .unwrap()
        .try_into()
        .unwrap();
    let [reclaimed] = store
        .reclaim_stale_checks(10_301, 300, 5)
        .await
        .unwrap()
        .try_into()
        .unwrap();
    assert!(
        store
            .persist_check_result(request(
                &completed(&reclaimed, 10_301),
                error(1),
                Some((zero(), state([0, 0, 123, 0, 0, 0, 0, 0]))),
            ))
            .await
            .unwrap()
    );
    let after_recovery = db.schedule(1).unwrap();
    assert_eq!(after_recovery, ("idle".to_owned(), 10_601, 0));

    let applied = store
        .persist_check_result(request(
            &completed(&original, 10_400),
            result(1, ResultStatus::Ok),
            Some((state([0, 0, 123, 0, 0, 0, 0, 0]), zero())),
        ))
        .await
        .unwrap();

    assert!(!applied);
    assert_eq!(db.schedule(1).unwrap(), after_recovery);
    assert_eq!(db.state_row(1), Some([0, 0, 123, 0, 0, 0, 0, 0]));
    assert_eq!(db.results().len(), 2);
}

#[tokio::test]
async fn result_only_persistence_never_mutates_anchor_check() {
    let db = TestDb::new();
    db.insert(row(1).next(123));
    let store = db.store();
    let mut snapshot = check(1);
    snapshot.name = "stale snapshot".to_owned();
    snapshot.next_check_time = 999;

    assert!(
        store
            .persist_check_result(PersistRequest {
                complete_check: false,
                ..request(&snapshot, error(1), Some((zero(), zero())))
            })
            .await
            .unwrap()
    );

    let name: String = db
        .raw()
        .query_row("SELECT name FROM health_check WHERE id = 1", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(name, "check 1");
    assert_eq!(db.schedule(1), Some(("idle".to_owned(), 123, 0)));
    assert_eq!(db.results().len(), 1);
}

#[tokio::test]
async fn disabled_claim_is_released_without_late_reenable() {
    let db = TestDb::new();
    db.insert(row(1));
    let store = db.store();
    let [claimed] = store
        .claim_due_checks(10_000, 5)
        .await
        .unwrap()
        .try_into()
        .unwrap();
    db.raw()
        .execute("UPDATE health_check SET disabled = 1 WHERE id = 1", [])
        .unwrap();
    let [reclaimed] = store
        .reclaim_stale_checks(10_301, 300, 5)
        .await
        .unwrap()
        .try_into()
        .unwrap();
    assert!(reclaimed.disabled);

    store
        .persist_check_result(request(
            &completed(&claimed, 10_400),
            result(1, ResultStatus::Ok),
            None,
        ))
        .await
        .unwrap();

    let disabled: bool = db
        .raw()
        .query_row(
            "SELECT disabled FROM health_check WHERE id = 1",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert!(disabled);
}

#[tokio::test]
async fn late_completion_does_not_resurrect_deleted_check() {
    let db = TestDb::new();
    db.insert(row(1));
    let store = db.store();
    let [claimed] = store
        .claim_due_checks(10_000, 5)
        .await
        .unwrap()
        .try_into()
        .unwrap();
    db.delete_check(1);

    let applied = store
        .persist_check_result(request(
            &completed(&claimed, 10_010),
            result(1, ResultStatus::Ok),
            None,
        ))
        .await
        .unwrap();

    assert!(!applied);
    assert_eq!(db.schedule(1), None);
    assert!(db.results().is_empty());
}

#[tokio::test]
async fn reclaimed_result_does_not_resurrect_deleted_check() {
    let db = TestDb::new();
    db.insert(row(1));
    let store = db.store();
    store.claim_due_checks(10_000, 5).await.unwrap();
    let [reclaimed] = store
        .reclaim_stale_checks(10_301, 300, 5)
        .await
        .unwrap()
        .try_into()
        .unwrap();
    db.delete_check(1);

    let applied = store
        .persist_check_result(request(&completed(&reclaimed, 10_301), error(1), None))
        .await
        .unwrap();

    assert!(!applied);
    assert!(db.results().is_empty());
}

#[tokio::test]
async fn result_only_persistence_for_a_deleted_check_is_dropped() {
    let db = TestDb::new();
    let store = db.store();

    let applied = store
        .persist_check_result(PersistRequest {
            complete_check: false,
            ..request(
                &check(1),
                error(1),
                Some((zero(), state([1, 0, 0, 0, 0, 0, 0, 0]))),
            )
        })
        .await
        .unwrap();

    assert!(!applied);
    assert!(db.results().is_empty());
    assert_eq!(db.state_row(1), None);
}

#[tokio::test]
async fn hold_marker_lands_atomically_with_the_completion() {
    let db = TestDb::new();
    db.insert(row(1).processing(4_000));
    let store = db.store();
    let mut done = check(1);
    done.next_check_time = 9_000;
    done.claim_started_at = 4_000;

    assert!(
        store
            .persist_check_result(hold(&done, 4_500))
            .await
            .unwrap()
    );

    assert_eq!(db.schedule(1), Some(("idle".to_owned(), 9_000, 0)));
    assert_eq!(db.state_row(1), Some([0, 0, 0, 0, 0, 4_500, 0, 0]));
}

#[tokio::test]
async fn hold_marker_never_overwrites_a_running_hold() {
    let db = TestDb::new();
    db.insert(row(1));
    db.set_state(1, state([3, 0, 0, 0, 800, 900, 2, 0]));
    let store = db.store();

    assert!(
        store
            .persist_check_result(hold(&check(1), 9_999))
            .await
            .unwrap()
    );

    assert_eq!(db.state_row(1), Some([3, 0, 0, 0, 800, 900, 2, 0]));
}

#[tokio::test]
async fn hold_marker_is_stamped_on_an_existing_unheld_row() {
    let db = TestDb::new();
    db.insert(row(1));
    db.set_state(1, state([3, 0, 0, 0, 800, 0, 2, 0]));
    let store = db.store();

    assert!(
        store
            .persist_check_result(hold(&check(1), 4_500))
            .await
            .unwrap()
    );

    assert_eq!(db.state_row(1), Some([3, 0, 0, 0, 800, 4_500, 2, 0]));
}

#[tokio::test]
async fn a_stale_completion_marks_nothing() {
    let db = TestDb::new();
    db.insert(row(1).processing(8_000));
    let store = db.store();
    let mut late = check(1);
    late.next_check_time = 9_000;
    late.claim_started_at = 4_000;

    assert!(
        !store
            .persist_check_result(hold(&late, 4_500))
            .await
            .unwrap()
    );

    assert_eq!(db.results().len(), 1);
    assert_eq!(db.state_row(1), None);
}

#[tokio::test]
async fn a_deleted_check_marks_nothing() {
    let db = TestDb::new();
    let store = db.store();
    let mut ghost = check(1);
    ghost.claim_started_at = 4_000;

    assert!(
        !store
            .persist_check_result(hold(&ghost, 4_500))
            .await
            .unwrap()
    );

    assert!(db.results().is_empty());
    assert_eq!(db.state_row(1), None);
}

#[tokio::test]
async fn hold_marker_is_ignored_when_a_transition_is_given() {
    let db = TestDb::new();
    db.insert(row(1));
    let store = db.store();

    assert!(
        store
            .persist_check_result(PersistRequest {
                transition: Some((zero(), state([1, 0, 0, 0, 800, 0, 0, 0]))),
                ..hold(&check(1), 4_500)
            })
            .await
            .unwrap()
    );

    assert_eq!(db.state_row(1), Some([1, 0, 0, 0, 800, 0, 0, 0]));
}

#[tokio::test]
async fn notification_state_is_deleted_with_its_check() {
    let db = TestDb::new();
    db.insert(row(1));
    let store = db.store();
    store
        .persist_check_result(request(
            &check(1),
            error(1),
            Some((zero(), state([2, 2, 0, 100, 90, 0, 0, 0]))),
        ))
        .await
        .unwrap();

    db.delete_check(1);

    assert_eq!(store.get_notification_state(1).await.unwrap(), zero());
}

#[tokio::test]
async fn nan_in_result_data_fails_json_valid_and_rolls_back() {
    let db = TestDb::new();
    db.insert(row(1).processing(4_000));
    let store = db.store();
    let mut claimed = check(1);
    claimed.claim_started_at = 4_000;
    let mut data = dict("{}");
    data.insert("value".to_owned(), PyJson::Float(f64::NAN));

    let err = store
        .persist_check_result(request(
            &completed(&claimed, 9_000),
            CheckResult::new(1, ResultStatus::Error, data),
            None,
        ))
        .await
        .unwrap_err();

    assert!(matches!(err, StoreError::Sqlite(_)), "{err:?}");
    assert_eq!(db.schedule(1), Some(("processing".to_owned(), 0, 4_000)));
    assert!(db.results().is_empty());
}
