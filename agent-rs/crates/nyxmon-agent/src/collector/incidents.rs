//! The collector's own incidents (`AsyncCheckCollector._sync_*`,
//! `_record_incident_send`, `_reconcile_incidents_after_restart`; PLAN.md
//! 5.8 steps 1, 4 and 5).
//!
//! Dedup and reminder state lives in the `collector_incident` table; the
//! retry set (`_incident_retry_keys`) is process-local and rehydrated from
//! the persisted `delivery_pending` marker. Every error is logged and
//! swallowed, like Python's `except Exception: logger.exception(...)`.

use std::fmt;

use num_bigint::BigInt;
use nyxmon_core::collector_incident::{
    PAUSED_INCIDENT_KEY, STALE_BATCH_INCIDENT_KEY, merge_stale_batch_payload, paused_batch_message,
    paused_batch_payload, stale_batch_message,
};
use nyxmon_core::incident::DELIVERY_PENDING_KEY;
use nyxmon_core::model::{CollectorIncident, ResultStatus};
use nyxmon_core::pyjson::{PyNumError, py_int_of, py_str, py_truthy};
use nyxmon_notify::Delivery;
use nyxmon_store::StoreError;

use super::{Collector, collector_log, locked};
use crate::incident_notification::{IncidentNotification, build_incident_notification};
use crate::runner::CatchUnwind;

/// `ABANDONED_BATCH_REMINDER_SECONDS`.
pub const ABANDONED_BATCH_REMINDER_SECONDS: i64 = 3600;
/// `ABANDONED_BATCH_RETRY_SECONDS`: the reminder interval while the last
/// alert of an incident was not delivered.
pub const ABANDONED_BATCH_RETRY_SECONDS: i64 = 60;
/// `STALE_BATCH_REMINDER_SECONDS`.
pub const STALE_BATCH_REMINDER_SECONDS: i64 = 3600;
/// `STALE_BATCH_RESOLVE_AFTER_SECONDS`: quiet period before a stale-lease
/// incident is resolved.
pub const STALE_BATCH_RESOLVE_AFTER_SECONDS: i64 = 900;

const STALE_NAME: &str = "Nyxmon collector (stale check leases)";
const STALE_ERROR_TYPE: &str = "stale_processing_lease_batch";
const PAUSED_NAME: &str = "Nyxmon collector (execution paused)";
const PAUSED_ERROR_TYPE: &str = "collector_execution_paused";

/// An exception escaping an incident update.
#[derive(Debug)]
enum IncidentError {
    Store(StoreError),
    /// A Python exception of a payload value (`int()`, `list()`).
    Py(PyNumError),
}

impl fmt::Display for IncidentError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            IncidentError::Store(err) => write!(f, "{err}"),
            IncidentError::Py(err) => write!(f, "{}: {err}", err.python_class()),
        }
    }
}

impl From<StoreError> for IncidentError {
    fn from(err: StoreError) -> Self {
        IncidentError::Store(err)
    }
}

impl From<PyNumError> for IncidentError {
    fn from(err: PyNumError) -> Self {
        IncidentError::Py(err)
    }
}

impl Collector {
    fn retry_pending(&self, incident_key: &str) -> bool {
        locked(&self.retry_keys).contains(incident_key)
    }

    /// `_incident_reminder_seconds`: retry sooner when the previous alert
    /// failed to send.
    fn reminder_seconds(&self, incident_key: &str, default: i64) -> i64 {
        if self.retry_pending(incident_key) {
            ABANDONED_BATCH_RETRY_SECONDS
        } else {
            default
        }
    }

    /// `_rehydrate_incident_retry_state`: restore the retry marker from the
    /// persisted payload after a restart.
    fn rehydrate_retry_state(&self, incident_key: &str, existing: Option<&CollectorIncident>) {
        if existing
            .and_then(|incident| incident.payload.get(DELIVERY_PENDING_KEY))
            .is_some_and(py_truthy)
        {
            locked(&self.retry_keys).insert(incident_key.to_owned());
        }
    }

    /// `_incident_close`: forget the retry marker, delete the row.
    async fn close_incident(
        &self,
        incident_key: &str,
    ) -> Result<Option<CollectorIncident>, StoreError> {
        locked(&self.retry_keys).remove(incident_key);
        self.store.close_collector_incident(incident_key).await
    }

    /// `_notify_collector_incident`: send one collector-scoped alert; only
    /// a failed send (or a raising or panicking notifier) counts as
    /// undelivered.
    async fn notify_incident(
        &self,
        incident_key: &str,
        name: &str,
        error_type: &str,
        error_msg: String,
        alert_count: i64,
    ) -> bool {
        let (check, result) = build_incident_notification(&IncidentNotification {
            incident_key,
            name,
            error_type,
            error_msg: &error_msg,
            status: ResultStatus::Error,
            opsgate_ticket: true,
            alert_count,
        });
        // A panicking notifier is a failed send (D-4), not the end of the
        // collector task.
        let notifier = self.handler.notifier();
        let send = CatchUnwind(Box::pin(async {
            notifier.notify_check_failed(&check, &result).await
        }));
        let failure = match send.await {
            Ok(Ok(delivery)) => return delivery != Delivery::Failed,
            Ok(Err(err)) => err.to_string(),
            Err(()) => "panic".to_owned(),
        };
        collector_log!(
            error,
            "failed to send the collector incident notification for {incident_key}\n{failure}"
        );
        false
    }

    /// `_record_incident_send`: a failed send only marks the incident for
    /// retry (the claim already wrote the intent); a delivered one is
    /// acknowledged, fenced by the attempt it delivered.
    pub(super) async fn record_incident_send(&self, incident_key: &str, sent: bool, attempt: i64) {
        if !sent {
            locked(&self.retry_keys).insert(incident_key.to_owned());
            return;
        }
        locked(&self.retry_keys).remove(incident_key);
        if let Err(err) = self
            .store
            .acknowledge_collector_incident_delivery(incident_key, attempt)
            .await
        {
            collector_log!(
                error,
                "failed to acknowledge the delivery of incident {incident_key}\n{err}"
            );
        }
    }

    /// Step 1 (`_reconcile_incidents_after_restart`): the abandoned batch
    /// of a previous process did not survive it.
    pub(super) async fn reconcile_incidents_after_restart(&self) {
        match self.close_incident(PAUSED_INCIDENT_KEY).await {
            Ok(Some(closed)) => collector_log!(
                warn,
                "collector execution-paused incident opened at {} was resolved on startup after \
                 {} alert(s); the abandoned batch thread did not survive the restart",
                closed.opened_at,
                closed.alert_count
            ),
            Ok(None) => {}
            Err(err) => {
                collector_log!(
                    error,
                    "failed to reconcile collector incidents on startup\n{err}"
                );
            }
        }
    }

    /// Step 4 (`_sync_stale_batch_incident`): fold a burst of expired
    /// leases into one deduplicated incident.
    pub(super) async fn sync_stale_batch_incident(&self, recovered: &[i64]) {
        if let Err(err) = self.try_sync_stale_batch_incident(recovered).await {
            collector_log!(
                error,
                "failed to update the stale processing-lease incident\n{err}"
            );
        }
    }

    async fn send_stale_alert(&self, incident: &CollectorIncident) -> Result<bool, IncidentError> {
        let message = stale_batch_message(incident)?;
        let sent = self
            .notify_incident(
                STALE_BATCH_INCIDENT_KEY,
                STALE_NAME,
                STALE_ERROR_TYPE,
                message,
                incident.alert_count,
            )
            .await;
        self.record_incident_send(STALE_BATCH_INCIDENT_KEY, sent, incident.alert_count)
            .await;
        Ok(sent)
    }

    async fn try_sync_stale_batch_incident(&self, recovered: &[i64]) -> Result<(), IncidentError> {
        let key = STALE_BATCH_INCIDENT_KEY;
        let now = self.handler.now();
        if !recovered.is_empty() {
            let existing = self.store.get_collector_incident(key).await?;
            self.rehydrate_retry_state(key, existing.as_ref());
            let payload = merge_stale_batch_payload(existing.as_ref(), recovered, now)?;
            let alert = self
                .store
                .claim_collector_incident_alert(
                    key,
                    now,
                    self.reminder_seconds(key, STALE_BATCH_REMINDER_SECONDS),
                    Some(payload),
                )
                .await?;
            collector_log!(
                warn,
                "reclaimed {} expired check processing lease(s); tracked as a single collector \
                 incident ({} alert(s) so far)",
                recovered.len(),
                alert.incident.alert_count
            );
            if alert.should_notify {
                self.send_stale_alert(&alert.incident).await?;
            }
            return Ok(());
        }

        let Some(existing) = self.store.get_collector_incident(key).await? else {
            return Ok(());
        };
        self.rehydrate_retry_state(key, Some(&existing));

        // Retry a delivery that failed earlier: a one-off batch produces no
        // further reclaims, so only this branch can still page.
        if self.retry_pending(key) {
            let alert = self
                .store
                .claim_collector_incident_alert(
                    key,
                    now,
                    self.reminder_seconds(key, STALE_BATCH_REMINDER_SECONDS),
                    None,
                )
                .await?;
            if alert.should_notify && !self.send_stale_alert(&alert.incident).await? {
                // Keep the incident open while delivery is still failing.
                return Ok(());
            }
        }

        // `int(payload.get("last_seen_at") or opened_at)`; TypeError and
        // ValueError fall back to `opened_at`.
        let last_seen = match existing
            .payload
            .get("last_seen_at")
            .filter(|value| py_truthy(value))
        {
            Some(value) => match py_int_of(value) {
                Ok(last_seen) => last_seen,
                Err(PyNumError::OverflowError(message)) => {
                    return Err(PyNumError::OverflowError(message).into());
                }
                Err(_) => BigInt::from(existing.opened_at),
            },
            None => BigInt::from(existing.opened_at),
        };
        if BigInt::from(now) - last_seen < BigInt::from(STALE_BATCH_RESOLVE_AFTER_SECONDS) {
            return Ok(());
        }
        if self.retry_pending(key) {
            // Never close an incident whose alert was never delivered.
            return Ok(());
        }
        self.close_incident(key).await?;
        let reclaimed = existing
            .payload
            .get("reclaimed_count")
            .map_or_else(|| "0".to_owned(), py_str);
        collector_log!(
            info,
            "stale processing-lease incident resolved after {reclaimed} reclaimed lease(s) and \
             {} alert(s)",
            existing.alert_count
        );
        Ok(())
    }

    /// Step 5 (`_sync_paused_batch_incident`): alert once for a wedged
    /// batch, then remind on elapsed time.
    pub(super) async fn sync_paused_batch_incident(&self) {
        if let Err(err) = self.try_sync_paused_batch_incident().await {
            collector_log!(
                error,
                "failed to update the collector execution-paused incident\n{err}"
            );
        }
    }

    async fn try_sync_paused_batch_incident(&self) -> Result<(), IncidentError> {
        let key = PAUSED_INCIDENT_KEY;
        let check_ids: Vec<i64> = locked(&self.abandoned)
            .as_ref()
            .map(|abandoned| {
                abandoned
                    .checks
                    .iter()
                    .map(|check| check.check_id)
                    .collect()
            })
            .unwrap_or_default();
        let alert = self
            .store
            .claim_collector_incident_alert(
                key,
                self.handler.now(),
                self.reminder_seconds(key, ABANDONED_BATCH_REMINDER_SECONDS),
                Some(paused_batch_payload(&check_ids)),
            )
            .await?;
        if !alert.should_notify {
            return Ok(());
        }
        collector_log!(
            error,
            "abandoned check batch is still running; new executions are paused"
        );
        let message = paused_batch_message(&alert.incident, self.handler.now())?;
        let sent = self
            .notify_incident(
                key,
                PAUSED_NAME,
                PAUSED_ERROR_TYPE,
                message,
                alert.incident.alert_count,
            )
            .await;
        self.record_incident_send(key, sent, alert.incident.alert_count)
            .await;
        Ok(())
    }

    /// `_resolve_paused_batch_incident`: the abandoned batch finished.
    pub(super) async fn resolve_paused_batch_incident(&self) {
        match self.close_incident(PAUSED_INCIDENT_KEY).await {
            Ok(Some(closed)) => collector_log!(
                info,
                "abandoned check batch finished; execution resumed and the collector incident \
                 was resolved after {} alert(s)",
                closed.alert_count
            ),
            Ok(None) => {}
            Err(err) => collector_log!(
                error,
                "failed to resolve the collector execution-paused incident\n{err}"
            ),
        }
    }
}
