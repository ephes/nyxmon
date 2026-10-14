//! The collector loop and its shutdown (`adapters/collector.py:
//! AsyncCheckCollector`, PLAN.md 5.8).
//!
//! One iteration (`_collect_once`): on the first one, close an
//! execution-paused incident left by a previous process; derive the
//! processing lease from the configured checks; reclaim expired leases,
//! record their collector-internal results and fold them into one
//! stale-lease incident; while an abandoned batch is still running, keep
//! execution paused and alert about it; otherwise claim due checks and run
//! them as one batch with a deadline. A batch that misses its deadline is
//! abandoned, not cancelled.
//!
//! Shutdown: stop admitting work on a signal, give a running (or
//! abandoned) batch the grace period, then stop its intake, record what
//! finished in time, abort the rest (D-3) and release the claims this
//! process still owns.

mod incidents;

use std::collections::HashSet;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use num_bigint::BigInt;
use nyxmon_checks::ctx::Sleeper;
use nyxmon_checks::registry::Executors;
use nyxmon_core::env::{Env, check_batch_size, processing_lease_seconds, shutdown_grace_seconds};
use nyxmon_core::lease::{MAX_DERIVED_PROCESSING_LEASE_SECONDS, estimated_check_runtime_seconds};
use nyxmon_core::model::{Check, CheckResult, ResultStatus};
use nyxmon_core::pyjson::{PyDict, PyJson};
use nyxmon_store::{Store, StoreError};
use tokio::task::JoinHandle;
use tokio::time::Instant;

use crate::results::{HandlerError, ResultHandler};
use crate::runner::{IntakeStop, run_batch};
use crate::sync::Flag;

/// `SHUTDOWN_FLUSH_SECONDS`: after the grace period, how long the results
/// that finished in time get to be recorded before the leases are released.
pub const SHUTDOWN_FLUSH_SECONDS: u64 = 15;
/// `RESULT_HANDLING_BUDGET_SECONDS`: added to the lease per claimed check
/// for the batch deadline.
pub const RESULT_HANDLING_BUDGET_SECONDS: i64 = 60;
/// `MAX_STALE_RECLAIM_ROUNDS`.
pub const MAX_STALE_RECLAIM_ROUNDS: usize = 20;

macro_rules! collector_log {
    ($level:ident, $($arg:tt)*) => {
        tracing::$level!(target: "nyxmon.adapters.collector", $($arg)*)
    };
}
pub(crate) use collector_log;

/// The runtime-derived lease (`_cached_required_lease` and its warnings).
#[derive(Debug, Default)]
struct LeaseEstimate {
    /// `_cached_required_lease`.
    cached_required: i64,
    /// `_runtime_budget_refresh_at` (monotonic); `None` before the first
    /// estimate.
    refresh_at: Option<Instant>,
    /// `_last_lease_warning`.
    last_warning: Option<(BigInt, i64)>,
    /// `_last_lease_cap_warning`.
    last_cap_warning: Option<i64>,
}

/// A batch that missed its deadline and still runs
/// (`_abandoned_batch_done`, `_abandoned_batch_checks`).
struct Abandoned {
    task: JoinHandle<()>,
    done: Arc<Flag>,
    checks: Vec<Check>,
}

/// Sets its flag when dropped: the batch task finished, panicked or was
/// aborted (`finally: batch_done.set()`).
struct DoneGuard(Arc<Flag>);

impl Drop for DoneGuard {
    fn drop(&mut self) {
        self.0.set();
    }
}

#[cfg(test)]
type RecoveryGate =
    Arc<dyn Fn() -> std::pin::Pin<Box<dyn Future<Output = ()> + Send>> + Send + Sync>;

/// The collector of one agent process.
pub struct Collector {
    store: Store,
    handler: Arc<ResultHandler<Store>>,
    executors: Arc<dyn Executors>,
    env: Arc<Env>,
    interval: i64,
    flush_timeout: Duration,
    /// Replaces the computed batch deadline (tests).
    batch_deadline: Option<Duration>,
    stop: Flag,
    fatal: Flag,
    intake: Arc<IntakeStop>,
    /// Claims of the batch in progress (`_active_batch_checks`).
    active: Mutex<Vec<Check>>,
    abandoned: Mutex<Option<Abandoned>>,
    lease: Mutex<LeaseEstimate>,
    /// `_incident_retry_keys`: incidents whose last alert was not delivered.
    retry_keys: Mutex<HashSet<String>>,
    /// `_startup_incidents_reconciled`.
    reconciled: AtomicBool,
    /// Awaited before each stale result is recorded (tests).
    #[cfg(test)]
    recovery_gate: Option<RecoveryGate>,
}

fn locked<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|e| e.into_inner())
}

/// Whole seconds as a [`Duration`], saturating.
fn seconds(value: &BigInt) -> Duration {
    u64::try_from(value).map_or(Duration::MAX, Duration::from_secs)
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
            batch_deadline: None,
            stop: Flag::new(),
            fatal: Flag::new(),
            intake: Arc::new(IntakeStop::default()),
            active: Mutex::new(Vec::new()),
            abandoned: Mutex::new(None),
            lease: Mutex::new(LeaseEstimate::default()),
            retry_keys: Mutex::new(HashSet::new()),
            reconciled: AtomicBool::new(false),
            #[cfg(test)]
            recovery_gate: None,
        }
    }

    /// Shorten the flush wait (tests).
    pub fn with_flush_timeout(mut self, timeout: Duration) -> Self {
        self.flush_timeout = timeout;
        self
    }

    /// Replace the computed batch deadline (tests).
    #[cfg(test)]
    pub(crate) fn with_batch_deadline(mut self, deadline: Duration) -> Self {
        self.batch_deadline = Some(deadline);
        self
    }

    /// Await `gate` before each stale result is recorded (tests).
    #[cfg(test)]
    pub(crate) fn with_recovery_gate(mut self, gate: RecoveryGate) -> Self {
        self.recovery_gate = Some(gate);
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

    fn active(&self) -> MutexGuard<'_, Vec<Check>> {
        locked(&self.active)
    }

    /// Whether an abandoned batch is still running.
    fn abandoned_running(&self) -> bool {
        locked(&self.abandoned)
            .as_ref()
            .is_some_and(|abandoned| !abandoned.done.is_set())
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
            let abandoned = locked(&self.abandoned).take();
            let mut claims = std::mem::take(&mut *self.active());
            if let Some(abandoned) = abandoned {
                // D-3: no result of the abandoned batch is written after
                // the release.
                abandoned.task.abort();
                let _ = abandoned.task.await;
                claims.extend(abandoned.checks);
            }
            let now = self.handler.now();
            if let Err(err) = self.store.release_claims(now, &claims).await {
                // Unreleased leases fall back to expiry recovery.
                collector_log!(error, "failed to release check leases on shutdown\n{err}");
            }
        }
    }

    /// The `shutdown_deadline` task: once stop is requested, give a running
    /// or abandoned batch the grace period, then stop its intake and wait
    /// (bounded) for the results that finished in time.
    async fn shutdown_deadline(&self) {
        self.stop.wait().await;
        if self.active().is_empty() && !self.abandoned_running() {
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
            collector_log!(
                warn,
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
        // A previously abandoned batch also gets the grace period.
        let done = locked(&self.abandoned)
            .as_ref()
            .map(|abandoned| abandoned.done.clone());
        if let Some(done) = done {
            tokio::select! {
                () = done.wait() => {}
                () = cancel.wait() => {}
            }
        }
    }

    /// One iteration of `_collect_once`.
    async fn collect_once(&self, cancel: &Flag) {
        if !self.reconciled.swap(true, Ordering::SeqCst) {
            self.reconcile_incidents_after_restart().await;
            // `_warn_unobserved_site_requirements` needs the site observer
            // (S11); without one it does nothing.
        }
        let effective_lease = self.effective_lease().await;
        // Reclaiming commits before the stale results and the batch alert
        // are written, so shutdown waits for recovery instead of
        // cancelling it halfway and losing that crash evidence.
        self.recover_stale_leases(&effective_lease).await;

        if self.abandoned_running() {
            tokio::select! {
                () = self.sync_paused_batch_incident() => {}
                () = cancel.wait() => {}
            }
            return;
        }
        if locked(&self.abandoned).take().is_some() {
            self.resolve_paused_batch_incident().await;
        }

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
                self.store_failed(&err);
                collector_log!(error, "check collector iteration failed\n{err}");
                return;
            }
        };
        *self.active() = checks.clone();
        if self.stop.is_set() || checks.is_empty() {
            return;
        }
        // Abandon the wait after the effective lease so this collector can
        // keep reaping and scheduling even if an executor wedges.
        let deadline_seconds =
            &effective_lease + BigInt::from(checks.len()) * RESULT_HANDLING_BUDGET_SECONDS;
        let deadline = self
            .batch_deadline
            .unwrap_or_else(|| seconds(&deadline_seconds));
        let done = Arc::new(Flag::new());
        let mut batch = {
            let guard = DoneGuard(done.clone());
            let handler = self.handler.clone();
            let executors = self.executors.clone();
            let intake = self.intake.clone();
            let checks = checks.clone();
            tokio::spawn(async move {
                let _guard = guard;
                run_batch(&executors, checks, Some(&intake), move |check, result| {
                    let handler = handler.clone();
                    async move { handler.receive(check, result).await }
                })
                .await;
            })
        };
        let mut abandon = false;
        tokio::select! {
            joined = &mut batch => {
                if let Err(err) = joined
                    && err.is_panic()
                {
                    collector_log!(error, "check collector iteration failed\npanic");
                }
            }
            () = tokio::time::sleep(deadline) => abandon = true,
            () = cancel.wait() => {
                // D-3: no result is written after the release.
                batch.abort();
                let _ = batch.await;
                return;
            }
        }
        // A finished batch is no longer released at shutdown; one that ended
        // through the intake stop keeps its claims for the release. An
        // abandoned batch keeps them in its own record.
        if !self.intake.requested.is_set() {
            self.active().clear();
        }
        if abandon {
            *locked(&self.abandoned) = Some(Abandoned {
                task: batch,
                done,
                checks,
            });
            collector_log!(
                error,
                "check batch exceeded its {deadline_seconds}s execution/result deadline; new \
                 executions are paused while lease recovery and alerts continue"
            );
        }
    }

    /// Steps 2 of `_collect_once`: `max(configured, required)`, the
    /// required lease re-estimated at most every `max(5, min(30,
    /// configured / 2))` seconds.
    async fn effective_lease(&self) -> BigInt {
        let configured = processing_lease_seconds(&self.env);
        let now = Instant::now();
        let due = locked(&self.lease)
            .refresh_at
            .is_none_or(|refresh_at| now >= refresh_at);
        let estimated = if due {
            match self.store.list_checks().await {
                Ok(checks) => Some(self.estimate_required_lease(&checks)),
                Err(err) => {
                    self.store_failed(&err);
                    collector_log!(
                        error,
                        "processing-lease runtime estimation failed; using the last safe budget\n\
                         {err}"
                    );
                    None
                }
            }
        } else {
            None
        };
        let mut lease = locked(&self.lease);
        if due && estimated.is_none() {
            // Python skips the warning below when the estimate raised.
            return configured.max(BigInt::from(lease.cached_required));
        }
        if let Some(estimated) = estimated {
            if estimated > MAX_DERIVED_PROCESSING_LEASE_SECONDS {
                if lease.last_cap_warning != Some(estimated) {
                    collector_log!(
                        warn,
                        "estimated maximum check runtime {estimated}s exceeds the derived lease \
                         ceiling {MAX_DERIVED_PROCESSING_LEASE_SECONDS}s; clamping the estimate"
                    );
                }
                lease.last_cap_warning = Some(estimated);
            } else {
                lease.last_cap_warning = None;
            }
            lease.cached_required = estimated.min(MAX_DERIVED_PROCESSING_LEASE_SECONDS);
            // `max(5, min(30, configured / 2))`; the configured lease is at
            // least 30 s.
            let refresh_interval = match i64::try_from(&configured) {
                Ok(configured) if configured < 60 => configured as f64 / 2.0,
                _ => 30.0,
            }
            .max(5.0);
            lease.refresh_at = Some(now + Duration::from_secs_f64(refresh_interval));
        }
        let required = BigInt::from(lease.cached_required);
        let effective = (&configured).max(&required).clone();
        if required > configured {
            let key = (configured.clone(), lease.cached_required);
            if lease.last_warning.as_ref() != Some(&key) {
                collector_log!(
                    warn,
                    "configured processing lease {configured}s is below the estimated maximum \
                     check runtime {required}s; using {effective}s"
                );
            }
            lease.last_warning = Some(key);
        } else {
            lease.last_warning = None;
        }
        effective
    }

    /// The largest runtime estimate of the enabled checks.
    fn estimate_required_lease(&self, checks: &[Check]) -> i64 {
        let warnings = self.handler.warnings();
        checks
            .iter()
            .filter(|check| !check.disabled)
            .map(|check| {
                estimated_check_runtime_seconds(
                    check.check_id,
                    &check.check_type,
                    &check.data,
                    Some(warnings),
                )
            })
            .max()
            .unwrap_or(0)
    }

    /// Step 3 (`_recover_stale_leases`): reclaim expired leases, record
    /// their results and sync the stale-lease incident.
    async fn recover_stale_leases(&self, effective_lease: &BigInt) {
        let mut stale_checks: Vec<Check> = Vec::new();
        for round in 0..MAX_STALE_RECLAIM_ROUNDS {
            let now = self.handler.now();
            // Python binds `stale_before = now - lease`; sqlite3 raises when
            // it leaves the 64-bit range (PLAN.md D-20). Inside it, a lease
            // beyond `i64::MAX` reclaims exactly what `i64::MAX` does.
            let reclaimed = if i64::try_from(BigInt::from(now) - effective_lease).is_ok() {
                let lease = i64::try_from(effective_lease).unwrap_or(i64::MAX);
                self.store
                    .reclaim_stale_checks(now, lease, check_batch_size(&self.env))
                    .await
            } else {
                collector_log!(
                    error,
                    "processing-lease reclaim failed\nOverflowError: Python int too large to \
                     convert to SQLite INTEGER"
                );
                break;
            };
            let batch = match reclaimed {
                Ok(batch) => batch,
                Err(err) => {
                    self.store_failed(&err);
                    collector_log!(error, "processing-lease reclaim failed\n{err}");
                    break;
                }
            };
            if batch.is_empty() {
                break;
            }
            stale_checks.extend(batch);
            if round == MAX_STALE_RECLAIM_ROUNDS - 1 {
                collector_log!(
                    warn,
                    "reclaimed {} expired leases without draining the backlog; the remainder is \
                     handled by the next collector iteration",
                    stale_checks.len()
                );
            }
        }

        let mut recovered: Vec<i64> = Vec::new();
        for check in stale_checks {
            let check_id = check.check_id;
            if !check.disabled && self.record_stale_check(check).await {
                recovered.push(check_id);
            }
        }
        self.sync_stale_batch_incident(&recovered).await;
    }

    /// `_record_stale_check`: record and reschedule one expired lease
    /// through the normal result path. The result is collector-internal, so
    /// it never notifies (section 5.3).
    async fn record_stale_check(&self, mut check: Check) -> bool {
        #[cfg(test)]
        if let Some(gate) = &self.recovery_gate {
            gate().await;
        }
        let mut data = PyDict::new();
        data.insert(
            "error_type".to_owned(),
            PyJson::from("stale_processing_lease"),
        );
        data.insert(
            "error_msg".to_owned(),
            PyJson::from(
                "Check processing lease expired; the previous worker did not persist a result",
            ),
        );
        data.insert("collector_internal".to_owned(), PyJson::Bool(true));
        let result = CheckResult::new(check.check_id, ResultStatus::Error, data);
        check.schedule_next_check(self.handler.now());
        match self.handler.add_check_result(&check, result, false).await {
            Ok(_) => true,
            Err(err) => {
                if let HandlerError::Store(err) = &err {
                    self.store_failed(err);
                }
                collector_log!(
                    error,
                    "failed to record expired processing lease for check_id={}\n{err}",
                    check.check_id
                );
                false
            }
        }
    }

    /// A store error: the database thread dying is fatal (PLAN.md 3.2).
    fn store_failed(&self, err: &StoreError) {
        if matches!(err, StoreError::ActorDied) {
            self.fatal.set();
            self.stop.set();
        }
    }
}

#[cfg(test)]
mod reliability_tests;
#[cfg(test)]
mod tests;
