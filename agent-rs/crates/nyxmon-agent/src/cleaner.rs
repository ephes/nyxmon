//! The results cleaner (`adapters/cleaner.py:AsyncResultsCleaner`, PLAN.md
//! 5.10).

use std::fmt;
use std::future::Future;

use nyxmon_checks::ctx::Sleeper;
use nyxmon_store::{Store, StoreError};

/// `DEFAULT_MAX_BATCHES_PER_CYCLE`.
pub const DEFAULT_MAX_BATCHES_PER_CYCLE: i64 = 100;

/// One cleaner batch of the store.
pub trait ResultsPruner: Send + Sync + 'static {
    /// `delete_old_results_async`: the number of deleted results.
    fn delete_old_results(
        &self,
        retention_seconds: i64,
        batch_size: i64,
    ) -> impl Future<Output = Result<usize, StoreError>> + Send;
}

impl ResultsPruner for Store {
    fn delete_old_results(
        &self,
        retention_seconds: i64,
        batch_size: i64,
    ) -> impl Future<Output = Result<usize, StoreError>> + Send {
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
    /// `batch_size` or `max_batches_per_cycle` below 1.
    pub fn new(
        store: S,
        interval: i64,
        retention_period: i64,
        batch_size: i64,
        max_batches_per_cycle: i64,
    ) -> Result<Self, CleanerConfigError> {
        if batch_size < 1 {
            return Err(CleanerConfigError("batch_size must be at least 1"));
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

    /// `run_cleanup_cycle`: delete expired results batch by batch until a
    /// batch comes back short, at most `max_batches_per_cycle` batches.
    ///
    /// # Errors
    /// The store error of a batch (the rows deleted before stay deleted).
    pub async fn run_cleanup_cycle(&self) -> Result<u64, StoreError> {
        let mut total_deleted: u64 = 0;
        let mut batches: i64 = 0;
        let mut drained = false;
        while batches < self.max_batches_per_cycle {
            let deleted = self
                .store
                .delete_old_results(self.retention_period, self.batch_size)
                .await?;
            batches += 1;
            let deleted = u64::try_from(deleted).unwrap_or(u64::MAX);
            total_deleted = total_deleted.saturating_add(deleted);
            if i128::from(deleted) < i128::from(self.batch_size) {
                drained = true;
                break;
            }
            // Let the collector write results between batches.
            tokio::task::yield_now().await;
        }
        if total_deleted > 0 {
            tracing::info!(
                target: "nyxmon.adapters.cleaner",
                "Cleaned up {total_deleted} old check results in {batches} batch(es)"
            );
        }
        if !drained {
            tracing::warn!(
                target: "nyxmon.adapters.cleaner",
                "Result cleanup stopped after {batches} batches ({total_deleted} rows); the \
                 remaining backlog is deleted next cycle"
            );
        }
        Ok(total_deleted)
    }

    /// `_async_start`: a cycle at once, then one every `interval` seconds,
    /// until the task is dropped.
    pub async fn run(&self) {
        let sleeper = Sleeper::real();
        loop {
            if let Err(err) = self.run_cleanup_cycle().await {
                tracing::error!(
                    target: "nyxmon.adapters.cleaner",
                    "Error during result cleanup: {err}"
                );
            }
            sleeper.sleep(self.interval as f64).await;
        }
    }
}

#[cfg(test)]
mod tests;
