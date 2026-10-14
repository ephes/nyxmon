//! Collector incidents (`tests/adapters/test_collector_incident_store.py`,
//! `test_collector_incident_delivery_ack.py`,
//! `test_site_connectivity_persistence.py`).

mod common;

use common::{TestDb, dict, dumps};
use nyxmon_store::Store;

const KEY: &str = "collector:stale_processing_lease";

#[tokio::test]
async fn collector_incident_lifecycle() {
    let db = TestDb::new();
    let store = db.store();
    assert_eq!(
        store.get_collector_incident("stale_batch").await.unwrap(),
        None
    );

    let first = store
        .claim_collector_incident_alert(
            "stale_batch",
            1_000,
            3600,
            Some(dict(r#"{"check_ids": [1, 2, 3]}"#)),
        )
        .await
        .unwrap();
    assert!(first.should_notify && first.is_new);
    assert_eq!(first.incident.opened_at, 1_000);

    for tick in [1_001, 1_500, 4_599] {
        let repeat = store
            .claim_collector_incident_alert("stale_batch", tick, 3600, None)
            .await
            .unwrap();
        assert!(!repeat.should_notify && !repeat.is_new);
        assert_eq!(repeat.incident.opened_at, 1_000);
        assert_eq!(repeat.incident.alert_count, 1);
        assert_eq!(repeat.incident.last_alert_at, 1_000);
        assert_eq!(
            dumps(&repeat.incident.payload),
            r#"{"check_ids": [1, 2, 3], "delivery_pending": true, "delivery_attempt": 1}"#
        );
    }

    let reminder = store
        .claim_collector_incident_alert("stale_batch", 4_600, 3600, None)
        .await
        .unwrap();
    assert!(reminder.should_notify && !reminder.is_new);
    assert_eq!(reminder.incident.alert_count, 2);
    assert_eq!(reminder.incident.opened_at, 1_000);

    let closed = store
        .close_collector_incident("stale_batch")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(closed.alert_count, 2);
    assert_eq!(
        store.get_collector_incident("stale_batch").await.unwrap(),
        None
    );
    assert_eq!(
        store.close_collector_incident("stale_batch").await.unwrap(),
        None
    );

    let again = store
        .claim_collector_incident_alert("stale_batch", 4_700, 3600, None)
        .await
        .unwrap();
    assert!(again.should_notify && again.is_new);
}

#[tokio::test]
async fn reminder_interval_is_at_least_one_second() {
    let db = TestDb::new();
    let store = db.store();
    store
        .claim_collector_incident_alert(KEY, 1_000, 0, None)
        .await
        .unwrap();

    let same_second = store
        .claim_collector_incident_alert(KEY, 1_000, 0, None)
        .await
        .unwrap();
    let next_second = store
        .claim_collector_incident_alert(KEY, 1_001, -5, None)
        .await
        .unwrap();

    assert!(!same_second.should_notify);
    assert!(next_second.should_notify);
}

#[tokio::test]
async fn collector_incident_dedup_survives_a_restart() {
    let db = TestDb::new();
    let opened = db
        .store()
        .claim_collector_incident_alert("collector_paused", 1_000, 3600, None)
        .await
        .unwrap();
    assert!(opened.should_notify);

    let restarted = Store::open(db.path()).unwrap();
    let surviving = restarted
        .get_collector_incident("collector_paused")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(surviving.opened_at, 1_000);
    let after = restarted
        .claim_collector_incident_alert("collector_paused", 1_010, 3600, None)
        .await
        .unwrap();
    assert!(!after.should_notify && !after.is_new);
    let later = restarted
        .claim_collector_incident_alert("collector_paused", 4_600, 3600, None)
        .await
        .unwrap();
    assert!(later.should_notify);
}

const OPENED_AT: i64 = 1_760_000_000;

#[tokio::test]
async fn open_collector_incident_is_silent() {
    let db = TestDb::new();
    let store = db.store();
    let key = "site:connectivity";

    let opened = store
        .open_collector_incident(key, OPENED_AT, dict(r#"{"paths": ["ipv4"]}"#))
        .await
        .unwrap();
    assert_eq!(
        (opened.opened_at, opened.last_alert_at, opened.alert_count),
        (OPENED_AT, 0, 0)
    );
    assert_eq!(dumps(&opened.payload), r#"{"paths": ["ipv4"]}"#);

    let refreshed = store
        .open_collector_incident(
            key,
            OPENED_AT + 9_999,
            dict(r#"{"paths": ["ipv4", "dns"]}"#),
        )
        .await
        .unwrap();
    assert_eq!(
        (
            refreshed.opened_at,
            refreshed.last_alert_at,
            refreshed.alert_count
        ),
        (OPENED_AT, 0, 0)
    );
    assert_eq!(dumps(&refreshed.payload), r#"{"paths": ["ipv4", "dns"]}"#);

    let claimed = store
        .claim_collector_incident_alert(key, OPENED_AT + 900, 21_600, None)
        .await
        .unwrap();
    assert!(claimed.should_notify && !claimed.is_new);
    assert_eq!(
        (claimed.incident.opened_at, claimed.incident.alert_count),
        (OPENED_AT, 1)
    );
    assert_eq!(
        dumps(&claimed.incident.payload),
        r#"{"paths": ["ipv4", "dns"], "delivery_pending": true, "delivery_attempt": 1}"#
    );

    let quiet = store
        .claim_collector_incident_alert(key, OPENED_AT + 1_000, 21_600, None)
        .await
        .unwrap();
    assert!(!quiet.should_notify);
}

#[tokio::test]
async fn claim_records_delivery_intent_and_markers_are_carried() {
    let db = TestDb::new();
    let store = db.store();
    let key = "stale_batch";

    let granted = store
        .claim_collector_incident_alert(key, 1_000, 3_600, Some(dict(r#"{"check_ids": [1]}"#)))
        .await
        .unwrap();
    assert!(granted.should_notify);
    assert_eq!(
        dumps(&granted.incident.payload),
        r#"{"check_ids": [1], "delivery_pending": true, "delivery_attempt": 1}"#
    );

    let refreshed = store
        .claim_collector_incident_alert(key, 1_100, 3_600, Some(dict(r#"{"check_ids": [1, 2]}"#)))
        .await
        .unwrap();
    assert!(!refreshed.should_notify);
    assert_eq!(
        dumps(&refreshed.incident.payload),
        r#"{"check_ids": [1, 2], "delivery_pending": true, "delivery_attempt": 1}"#
    );

    let reopened = store
        .open_collector_incident(key, 1_200, dict(r#"{"check_ids": [3]}"#))
        .await
        .unwrap();
    assert_eq!(
        dumps(&reopened.payload),
        r#"{"check_ids": [3], "delivery_pending": true, "delivery_attempt": 1}"#
    );

    let replaced = store
        .set_collector_incident_payload(key, dict(r#"{"check_ids": [3]}"#))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(dumps(&replaced.payload), r#"{"check_ids": [3]}"#);
    assert_eq!(replaced.alert_count, 1);
    let stored = store.get_collector_incident(key).await.unwrap().unwrap();
    assert_eq!(dumps(&stored.payload), r#"{"check_ids": [3]}"#);

    let reminder = store
        .claim_collector_incident_alert(key, 4_700, 3_600, None)
        .await
        .unwrap();
    assert!(reminder.should_notify);
    assert_eq!(
        dumps(&reminder.incident.payload),
        r#"{"check_ids": [3], "delivery_pending": true, "delivery_attempt": 2}"#
    );
    assert_eq!(
        store
            .set_collector_incident_payload("missing", dict("{}"))
            .await
            .unwrap(),
        None
    );
}

#[tokio::test]
async fn delivery_intent_is_committed_with_the_claim() {
    let db = TestDb::new();
    db.store()
        .claim_collector_incident_alert(
            "stale_batch",
            1_000,
            3_600,
            Some(dict(r#"{"check_ids": [1]}"#)),
        )
        .await
        .unwrap();

    let (last_alert_at, alert_count, payload): (i64, i64, String) = db
        .raw()
        .query_row(
            "SELECT last_alert_at, alert_count, payload FROM collector_incident
             WHERE incident_key = 'stale_batch'",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .unwrap();
    assert_eq!((last_alert_at, alert_count), (1_000, 1));
    assert_eq!(
        payload,
        r#"{"check_ids": [1], "delivery_pending": true, "delivery_attempt": 1}"#
    );
}

#[tokio::test]
async fn acknowledgement_clears_only_the_delivery_markers() {
    let db = TestDb::new();
    let store = db.store();
    store
        .claim_collector_incident_alert(
            KEY,
            1_000,
            3_600,
            Some(dict(r#"{"reclaimed_count": 3, "check_ids": [1, 2]}"#)),
        )
        .await
        .unwrap();

    assert!(
        store
            .acknowledge_collector_incident_delivery(KEY, 1)
            .await
            .unwrap()
    );

    let incident = store.get_collector_incident(KEY).await.unwrap().unwrap();
    assert_eq!(
        dumps(&incident.payload),
        r#"{"reclaimed_count": 3, "check_ids": [1, 2]}"#
    );
    assert_eq!((incident.alert_count, incident.last_alert_at), (1, 1_000));
}

#[tokio::test]
async fn acknowledgement_is_fenced_by_the_attempt() {
    let db = TestDb::new();
    let store = db.store();
    assert!(
        !store
            .acknowledge_collector_incident_delivery(KEY, 1)
            .await
            .unwrap()
    );

    store
        .claim_collector_incident_alert(KEY, 1_000, 3_600, Some(dict(r#"{"n": 1}"#)))
        .await
        .unwrap();
    store
        .claim_collector_incident_alert(KEY, 9_000, 3_600, Some(dict(r#"{"n": 2}"#)))
        .await
        .unwrap();

    assert!(
        !store
            .acknowledge_collector_incident_delivery(KEY, 1)
            .await
            .unwrap()
    );
    let incident = store.get_collector_incident(KEY).await.unwrap().unwrap();
    assert_eq!(
        dumps(&incident.payload),
        r#"{"n": 2, "delivery_pending": true, "delivery_attempt": 2}"#
    );
}

#[tokio::test]
async fn acknowledgement_compares_with_python_equality() {
    let db = TestDb::new();
    let store = db.store();
    store
        .open_collector_incident(KEY, 1_000, dict(r#"{"delivery_attempt": 2.0, "x": 1}"#))
        .await
        .unwrap();

    // `2.0 == 2` in Python.
    assert!(
        store
            .acknowledge_collector_incident_delivery(KEY, 2)
            .await
            .unwrap()
    );
    let incident = store.get_collector_incident(KEY).await.unwrap().unwrap();
    assert_eq!(dumps(&incident.payload), r#"{"x": 1}"#);
}

#[tokio::test]
async fn undecodable_payloads_read_as_empty() {
    let db = TestDb::new();
    let store = db.store();
    let connection = db.raw();
    for (key, payload) in [("a", "not json"), ("b", "[1, 2]"), ("c", ""), ("d", "null")] {
        connection
            .execute(
                "INSERT INTO collector_incident (incident_key, opened_at, last_alert_at, alert_count, payload)
                 VALUES (?, 5, 6, 7, ?)",
                [key, payload],
            )
            .unwrap();
    }

    for key in ["a", "b", "c", "d"] {
        let incident = store.get_collector_incident(key).await.unwrap().unwrap();
        assert_eq!(
            (
                incident.opened_at,
                incident.last_alert_at,
                incident.alert_count
            ),
            (5, 6, 7)
        );
        assert!(incident.payload.is_empty(), "{key}");
    }
    // A claim on an undecodable payload starts from `{}`.
    let claimed = store
        .claim_collector_incident_alert("a", 10_000, 60, None)
        .await
        .unwrap();
    assert_eq!(
        dumps(&claimed.incident.payload),
        r#"{"delivery_pending": true, "delivery_attempt": 8}"#
    );
}
