//! Counterparts of `tests/unit/cleaner_test.py`.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use super::*;
use crate::logging;
use crate::testutil::TestDb;

/// Reports a fixed sequence of deleted counts (`_FakeResults`); an
/// `Err` entry fails that batch.
#[derive(Clone, Default)]
struct FakeResults {
    counts: Arc<Mutex<Vec<Result<usize, ()>>>>,
    calls: Arc<Mutex<Vec<(i64, i64)>>>,
}

impl FakeResults {
    fn new(counts: impl IntoIterator<Item = usize>) -> Self {
        let results = FakeResults::default();
        *results.counts.lock().unwrap() = counts.into_iter().map(Ok).collect();
        results
    }

    fn calls(&self) -> Vec<(i64, i64)> {
        self.calls.lock().unwrap().clone()
    }
}

impl ResultsPruner for FakeResults {
    async fn delete_old_results(&self, retention: i64, batch: i64) -> Result<usize, StoreError> {
        self.calls.lock().unwrap().push((retention, batch));
        let mut counts = self.counts.lock().unwrap();
        if counts.is_empty() {
            return Ok(0);
        }
        counts
            .remove(0)
            .map_err(|()| StoreError::Json("database is locked".to_owned()))
    }
}

fn cleaner(results: &FakeResults, retention: i64, batch: i64, max: i64) -> Cleaner<FakeResults> {
    Cleaner::new(results.clone(), 3600, retention, batch, max).unwrap()
}

#[tokio::test]
async fn a_cycle_drains_full_batches_until_a_short_batch() {
    let results = FakeResults::new([100, 100, 100, 37]);
    let cleaner = cleaner(&results, 60, 100, DEFAULT_MAX_BATCHES_PER_CYCLE);
    assert_eq!(cleaner.run_cleanup_cycle().await.unwrap(), 337);
    assert_eq!(results.calls(), [(60, 100); 4]);
}

#[tokio::test]
async fn a_cycle_stops_after_one_call_when_nothing_expired() {
    let results = FakeResults::new([0]);
    let cleaner = cleaner(&results, 86400, 100, DEFAULT_MAX_BATCHES_PER_CYCLE);
    let (capture, _guard) = logging::capture();
    assert_eq!(cleaner.run_cleanup_cycle().await.unwrap(), 0);
    assert_eq!(results.calls().len(), 1);
    assert!(capture.lines().is_empty());
}

#[tokio::test]
async fn the_cycle_cap_stops_a_runaway_loop() {
    let results = FakeResults::new([50; 1000]);
    let cleaner = cleaner(&results, 86400, 50, 3);
    let (capture, _guard) = logging::capture();
    assert_eq!(cleaner.run_cleanup_cycle().await.unwrap(), 150);
    assert_eq!(results.calls().len(), 3);
    assert_eq!(
        capture.messages(),
        [
            "Cleaned up 150 old check results in 3 batch(es)",
            "Result cleanup stopped after 3 batches (150 rows); the remaining backlog is \
             deleted next cycle",
        ]
    );
    assert_eq!(capture.lines()[1].level, "WARNING");
    assert_eq!(capture.lines()[1].logger, "nyxmon.adapters.cleaner");
}

#[tokio::test]
async fn a_cycle_logs_the_total_deleted() {
    let results = FakeResults::new([10, 4]);
    let cleaner = cleaner(&results, 86400, 10, DEFAULT_MAX_BATCHES_PER_CYCLE);
    let (capture, _guard) = logging::capture();
    cleaner.run_cleanup_cycle().await.unwrap();
    assert_eq!(
        capture.lines()[0],
        logging::LogLine {
            level: "INFO",
            logger: "nyxmon.adapters.cleaner".to_owned(),
            message: "Cleaned up 14 old check results in 2 batch(es)".to_owned(),
        }
    );
}

#[test]
fn non_positive_limits_are_rejected() {
    let results = FakeResults::default();
    assert_eq!(
        Cleaner::new(results.clone(), 3600, 86400, 0, 100).err(),
        Some(CleanerConfigError("batch_size must be at least 1"))
    );
    assert_eq!(
        Cleaner::new(results, 3600, 86400, 1, 0).err(),
        Some(CleanerConfigError(
            "max_batches_per_cycle must be at least 1"
        ))
    );
}

#[tokio::test(start_paused = true)]
async fn the_loop_runs_a_cycle_at_once_and_survives_errors() {
    let results = FakeResults::default();
    *results.counts.lock().unwrap() = vec![Ok(5), Ok(5), Ok(2), Err(()), Ok(1)];
    let cleaner = Cleaner::new(results.clone(), 3600, 86400, 5, 100).unwrap();
    let (capture, _guard) = logging::capture();
    let run = cleaner.run();
    tokio::pin!(run);
    // The first cycle runs at once.
    assert!(
        tokio::time::timeout(Duration::from_secs(1), &mut run)
            .await
            .is_err()
    );
    assert_eq!(results.calls().len(), 3);
    // The next cycle, an interval later, fails and is logged; the one after
    // runs normally.
    assert!(
        tokio::time::timeout(Duration::from_secs(7200), &mut run)
            .await
            .is_err()
    );
    assert_eq!(results.calls().len(), 5);
    assert_eq!(
        capture.messages(),
        [
            "Cleaned up 12 old check results in 3 batch(es)",
            "Error during result cleanup: database is locked",
            "Cleaned up 1 old check results in 1 batch(es)",
        ]
    );
}

#[tokio::test]
async fn a_cycle_drains_an_sqlite_backlog() {
    let db = TestDb::new();
    db.execute(
        "INSERT INTO health_check (id, service_id, name, check_type, url, check_interval,
            next_check_time, processing_started_at, status, disabled, data)
         VALUES (1, 1, 'c', 'http', 'https://example.test', 60, 0, 0, 'idle', 0, '{}')",
    );
    let connection = db.raw();
    for id in 1..=2510 {
        let age = if id <= 2500 { "-2 days" } else { "-1 minutes" };
        connection
            .execute(
                "INSERT INTO check_result (id, health_check_id, status, data, created_at)
                 VALUES (?, 1, 'ok', '{}', datetime('now', ?))",
                rusqlite::params![id, age],
            )
            .unwrap();
    }
    let cleaner = Cleaner::new(db.store(), 3600, 86400, 1000, 100).unwrap();
    assert_eq!(cleaner.run_cleanup_cycle().await.unwrap(), 2500);
    let mut statement = connection
        .prepare("SELECT id FROM check_result ORDER BY id")
        .unwrap();
    let remaining: Vec<i64> = statement
        .query_map([], |row| row.get(0))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap();
    assert_eq!(remaining, (2501..=2510).collect::<Vec<_>>());
}
