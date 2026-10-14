//! The collector loop and its shutdown (`adapters/collector.py:
//! AsyncCheckCollector`, PLAN.md 5.8).
//!
//! S9 covers the basic loop (claim, execute, sleep) and the shutdown part:
//! stop admitting work on a signal, give a running batch the grace period,
//! then stop its intake, record what finished in time, abort the rest
//! (D-3) and release the claims this process still owns. Stale-lease
//! recovery, the runtime-derived lease, the batch deadline and the
//! collector incidents follow in S10.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use nyxmon_checks::ctx::Sleeper;
use nyxmon_checks::registry::Executors;
use nyxmon_core::env::{Env, check_batch_size, shutdown_grace_seconds};
use nyxmon_core::model::Check;
use nyxmon_store::{Store, StoreError};

use crate::results::ResultHandler;
use crate::runner::{IntakeStop, run_batch};
use crate::sync::Flag;

/// `SHUTDOWN_FLUSH_SECONDS`: after the grace period, how long the results
/// that finished in time get to be recorded before the leases are released.
pub const SHUTDOWN_FLUSH_SECONDS: u64 = 15;

/// The collector of one agent process.
pub struct Collector {
    store: Store,
    handler: Arc<ResultHandler<Store>>,
    executors: Arc<dyn Executors>,
    env: Arc<Env>,
    interval: i64,
    flush_timeout: Duration,
    stop: Flag,
    fatal: Flag,
    intake: Arc<IntakeStop>,
    /// Claims of the batch in progress (`_active_batch_checks`).
    active: Mutex<Vec<Check>>,
}

impl Collector {
    pub fn new(
        store: Store,
        handler: Arc<ResultHandler<Store>>,
        executors: Arc<dyn Executors>,
        env: Arc<Env>,
        interval: i64,
    ) -> Self {
        Collector {
            store,
            handler,
            executors,
            env,
            interval,
            flush_timeout: Duration::from_secs(SHUTDOWN_FLUSH_SECONDS),
            stop: Flag::new(),
            fatal: Flag::new(),
            intake: Arc::new(IntakeStop::default()),
            active: Mutex::new(Vec::new()),
        }
    }

    /// Shorten the flush wait (tests).
    pub fn with_flush_timeout(mut self, timeout: Duration) -> Self {
        self.flush_timeout = timeout;
        self
    }

    /// `request_stop`: admit no new work; [`Collector::run`] drains and
    /// releases.
    pub fn request_stop(&self) {
        self.stop.set();
    }

    /// Whether the collector ended because the database thread died
    /// (PLAN.md 3.2: fatal, exit 1).
    pub fn failed(&self) -> bool {
        self.fatal.is_set()
    }

    fn active(&self) -> std::sync::MutexGuard<'_, Vec<Check>> {
        self.active.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// `_async_start`: collect until stopped, then release the claims of a
    /// batch that did not finish.
    pub async fn run(self: Arc<Self>) {
        let cancel = Flag::new();
        {
            let collect = self.collect_loop(&cancel);
            let deadline = async {
                self.shutdown_deadline().await;
                cancel.set();
                std::future::pending::<()>().await;
            };
            tokio::select! {
                () = collect => {}
                () = deadline => {}
            }
        }
        if self.stop.is_set() {
            let claims = std::mem::take(&mut *self.active());
            let now = self.handler.now();
            if let Err(err) = self.store.release_claims(now, &claims).await {
                // Unreleased leases fall back to expiry recovery.
                tracing::error!(
                    target: "nyxmon.adapters.collector",
                    "failed to release check leases on shutdown\n{err}"
                );
            }
        }
    }

    /// The `shutdown_deadline` task: once stop is requested, give a running
    /// batch the grace period, then stop its intake and wait (bounded) for
    /// the results that finished in time.
    async fn shutdown_deadline(&self) {
        self.stop.wait().await;
        if self.active().is_empty() {
            return;
        }
        Sleeper::real()
            .sleep(shutdown_grace_seconds(&self.env))
            .await;
        self.intake.requested.set();
        if tokio::time::timeout(self.flush_timeout, self.intake.drained.wait())
            .await
            .is_err()
        {
            tracing::warn!(
                target: "nyxmon.adapters.collector",
                "results that finished during the shutdown grace period were not all recorded \
                 within {SHUTDOWN_FLUSH_SECONDS}s; releasing the remaining leases"
            );
        }
    }

    async fn collect_loop(&self, cancel: &Flag) {
        let sleeper = Sleeper::real();
        while !self.stop.is_set() {
            self.collect_once(cancel).await;
            if cancel.is_set() {
                return;
            }
            if !self.stop.is_set() {
                tokio::select! {
                    () = sleeper.sleep(self.interval as f64) => {}
                    () = cancel.wait() => return,
                }
            }
        }
    }

    /// One iteration of `_collect_once` (S9 part).
    async fn collect_once(&self, cancel: &Flag) {
        if self.stop.is_set() {
            return;
        }
        // The claim is never cut short: claimed rows must reach the active
        // set so that shutdown releases them.
        let now = self.handler.now();
        let checks = match self
            .store
            .claim_due_checks(now, check_batch_size(&self.env))
            .await
        {
            Ok(checks) => checks,
            Err(err) => {
                self.iteration_failed(&err);
                return;
            }
        };
        *self.active() = checks.clone();
        if self.stop.is_set() || checks.is_empty() {
            return;
        }
        let handler = self.handler.clone();
        let executors = self.executors.clone();
        let intake = self.intake.clone();
        let mut batch = tokio::spawn(async move {
            run_batch(&executors, checks, Some(&intake), move |check, result| {
                let handler = handler.clone();
                async move { handler.receive(check, result).await }
            })
            .await;
        });
        tokio::select! {
            joined = &mut batch => {
                if let Err(err) = joined
                    && err.is_panic()
                {
                    tracing::error!(
                        target: "nyxmon.adapters.collector",
                        "check collector iteration failed\npanic"
                    );
                }
            }
            () = cancel.wait() => {
                // D-3: no result is written after the release.
                batch.abort();
                let _ = batch.await;
                return;
            }
        }
        // A finished batch is no longer released at shutdown; one that ended
        // through the intake stop keeps its claims for the release.
        if !self.intake.requested.is_set() {
            self.active().clear();
        }
    }

    fn iteration_failed(&self, err: &StoreError) {
        tracing::error!(
            target: "nyxmon.adapters.collector",
            "check collector iteration failed\n{err}"
        );
        if matches!(err, StoreError::ActorDied) {
            self.fatal.set();
            self.stop.set();
        }
    }
}

#[cfg(test)]
mod tests;
