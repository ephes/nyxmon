//! The results cleaner (`adapters/cleaner.py:AsyncResultsCleaner`, PLAN.md
//! 5.10).

use std::fmt;
use std::future::Future;

use nyxmon_checks::ctx::Sleeper;
use nyxmon_store::{CleanupBatch, MAX_CLEANUP_BATCH_SIZE, Store, StoreError};

/// `DEFAULT_MAX_BATCHES_PER_CYCLE`.
pub const DEFAULT_MAX_BATCHES_PER_CYCLE: i64 = 100;

/// One cleaner batch of the store.
pub trait ResultsPruner: Send + Sync + 'static {
    /// `delete_old_results_async`: separate result and session deletion counts.
    /// A result failure with committed session deletions carries their count
    /// in `StoreError::CleanupAfterSessionCommit`.
    fn delete_old_results(
        &self,
        retention_seconds: i64,
        batch_size: i64,
    ) -> impl Future<Output = Result<CleanupBatch, StoreError>> + Send;
}

impl ResultsPruner for Store {
    fn delete_old_results(
        &self,
        retention_seconds: i64,
        batch_size: i64,
    ) -> impl Future<Output = Result<CleanupBatch, StoreError>> + Send {
        Store::delete_old_results(self, retention_seconds, batch_size)
    }
}

/// The `ValueError` of the constructor.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CleanerConfigError(pub &'static str);

impl fmt::Display for CleanerConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "ValueError: {}", self.0)
    }
}

impl std::error::Error for CleanerConfigError {}

/// The cleaner.
pub struct Cleaner<S> {
    store: S,
    interval: i64,
    retention_period: i64,
    batch_size: i64,
    max_batches_per_cycle: i64,
}

impl<S: ResultsPruner> Cleaner<S> {
    /// `AsyncResultsCleaner(interval=..., retention_period=..., batch_size=...)`.
    ///
    /// # Errors
    /// Any interval, retention or batch limit below 1, or a batch size above 1000.
    pub fn new(
        store: S,
        interval: i64,
        retention_period: i64,
        batch_size: i64,
        max_batches_per_cycle: i64,
    ) -> Result<Self, CleanerConfigError> {
        if retention_period < 1 {
            return Err(CleanerConfigError("retention_period must be at least 1"));
        }
        if interval < 1 {
            return Err(CleanerConfigError("interval must be at least 1"));
        }
        if !(1..=MAX_CLEANUP_BATCH_SIZE).contains(&batch_size) {
            return Err(CleanerConfigError("batch_size must be in 1..=1000"));
        }
        if max_batches_per_cycle < 1 {
            return Err(CleanerConfigError(
                "max_batches_per_cycle must be at least 1",
            ));
        }
        Ok(Cleaner {
            store,
            interval,
            retention_period,
            batch_size,
            max_batches_per_cycle,
        })
    }

    /// Delete expired rows until both tables come back short, at most
    /// `max_batches_per_cycle` batches. Returns the number of results deleted.
    ///
    /// # Errors
    /// Logs committed deletions, including sessions from a partially completed
    /// batch, and returns the original store error if a batch fails.
    pub async fn run_cleanup_cycle(&self) -> Result<u64, StoreError> {
        let batch_size = usize::try_from(self.batch_size).expect("validated batch size");
        let mut total_results: u64 = 0;
        let mut total_sessions: u64 = 0;
        let mut batches: i64 = 0;
        let mut drained = false;
        while batches < self.max_batches_per_cycle {
            let batch = match self
                .store
                .delete_old_results(self.retention_period, self.batch_size)
                .await
            {
                Ok(batch) => batch,
                Err(err) => {
                    let err = match err {
                        StoreError::CleanupAfterSessionCommit {
                            sessions_deleted,
                            error,
                        } => {
                            total_sessions = total_sessions.saturating_add(
                                u64::try_from(sessions_deleted).unwrap_or(u64::MAX),
                            );
                            StoreError::Sqlite(error)
                        }
                        err => err,
                    };
                    tracing::error!(
                        target: "nyxmon.adapters.cleaner",
                        "Error during result cleanup after {batches} completed batch(es) \
                         ({total_results} check results, {total_sessions} expired sessions): {err}"
                    );
                    return Err(err);
                }
            };
            batches += 1;
            total_results = total_results
                .saturating_add(u64::try_from(batch.results_deleted).unwrap_or(u64::MAX));
            total_sessions = total_sessions
                .saturating_add(u64::try_from(batch.sessions_deleted).unwrap_or(u64::MAX));
            if batch.results_deleted < batch_size && batch.sessions_deleted < batch_size {
                drained = true;
                break;
            }
            // Let the collector write results between batches.
            tokio::task::yield_now().await;
        }
        if total_results > 0 || total_sessions > 0 {
            tracing::info!(
                target: "nyxmon.adapters.cleaner",
                "Cleaned up {total_results} old check results and {total_sessions} expired sessions \
                 in {batches} batch(es)"
            );
        }
        if !drained {
            tracing::warn!(
                target: "nyxmon.adapters.cleaner",
                "Result cleanup stopped after {batches} batches ({total_results} check results, \
                 {total_sessions} expired sessions); the remaining backlog is deleted next cycle"
            );
        }
        Ok(total_results)
    }

    /// `_async_start`: a cycle at once, then one every `interval` seconds,
    /// until the task is dropped.
    pub async fn run(&self) {
        let sleeper = Sleeper::real();
        loop {
            // The cycle logs failures with its progress before returning.
            let _ = self.run_cleanup_cycle().await;
            sleeper.sleep(self.interval as f64).await;
        }
    }
}

#[cfg(test)]
mod tests;
