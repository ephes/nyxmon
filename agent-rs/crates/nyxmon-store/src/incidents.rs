//! Collector incidents (`SqliteStore.*_collector_incident*`; PLAN.md
//! section 3.4). The delivery-marker helpers are `nyxmon_core::incident`.

use nyxmon_core::PyJson;
use nyxmon_core::incident::{
    DELIVERY_ATTEMPT_KEY, carry_delivery_markers, with_delivery_intent, without_delivery_markers,
};
use nyxmon_core::model::{CollectorIncident, CollectorIncidentAlert};
use nyxmon_core::pyjson::{CmpOp, PyDict, py_cmp};
use rusqlite::{Connection, Transaction, TransactionBehavior, params};

use crate::actor::Store;
use crate::error::StoreError;
use crate::rows::select_incident;

fn dump(payload: &PyDict) -> Result<String, StoreError> {
    // `json.dumps(dict)`; the clone is the price of `PyJson`'s owned layout.
    PyJson::Dict(payload.clone())
        .to_json_string()
        .map_err(|err| StoreError::Json(err.to_string()))
}

fn immediate(connection: &mut Connection) -> Result<Transaction<'_>, StoreError> {
    Ok(connection.transaction_with_behavior(TransactionBehavior::Immediate)?)
}

/// `_claim_collector_incident_alert_async`.
fn claim_alert(
    connection: &mut Connection,
    incident_key: &str,
    now: i64,
    reminder_seconds: i64,
    payload: Option<PyDict>,
) -> Result<CollectorIncidentAlert, StoreError> {
    let transaction = immediate(connection)?;
    let (incident, should_notify, is_new) = match select_incident(&transaction, incident_key)? {
        None => (
            CollectorIncident {
                incident_key: incident_key.to_owned(),
                opened_at: now,
                last_alert_at: now,
                alert_count: 1,
                payload: with_delivery_intent(&payload.unwrap_or_default(), 1),
            },
            true,
            true,
        ),
        Some(existing) => {
            let elapsed = i128::from(now) - i128::from(existing.last_alert_at);
            let should_notify = elapsed >= i128::from(reminder_seconds.max(1));
            let alert_count = if should_notify {
                existing.alert_count.saturating_add(1)
            } else {
                existing.alert_count
            };
            let next_payload = payload.as_ref().unwrap_or(&existing.payload);
            let payload = if should_notify {
                with_delivery_intent(next_payload, alert_count)
            } else {
                carry_delivery_markers(next_payload, &existing.payload)
            };
            let incident = CollectorIncident {
                incident_key: incident_key.to_owned(),
                opened_at: existing.opened_at,
                last_alert_at: if should_notify {
                    now
                } else {
                    existing.last_alert_at
                },
                alert_count,
                payload,
            };
            (incident, should_notify, false)
        }
    };
    transaction.execute(
        "INSERT INTO collector_incident
                (incident_key, opened_at, last_alert_at, alert_count, payload)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(incident_key) DO UPDATE SET
                last_alert_at = excluded.last_alert_at,
                alert_count = excluded.alert_count,
                payload = excluded.payload",
        params![
            incident.incident_key,
            incident.opened_at,
            incident.last_alert_at,
            incident.alert_count,
            dump(&incident.payload)?,
        ],
    )?;
    transaction.commit()?;
    Ok(CollectorIncidentAlert {
        incident,
        should_notify,
        is_new,
    })
}

/// `_open_collector_incident_async`: create without claiming an alert, or
/// replace the payload carrying the delivery markers forward.
fn open_incident(
    connection: &mut Connection,
    incident_key: &str,
    now: i64,
    payload: PyDict,
) -> Result<CollectorIncident, StoreError> {
    let transaction = immediate(connection)?;
    let incident = match select_incident(&transaction, incident_key)? {
        None => {
            let incident = CollectorIncident {
                incident_key: incident_key.to_owned(),
                opened_at: now,
                last_alert_at: 0,
                alert_count: 0,
                payload,
            };
            transaction.execute(
                "INSERT INTO collector_incident
                        (incident_key, opened_at, last_alert_at, alert_count, payload)
                    VALUES (?, ?, ?, ?, ?)",
                params![
                    incident.incident_key,
                    incident.opened_at,
                    incident.last_alert_at,
                    incident.alert_count,
                    dump(&incident.payload)?,
                ],
            )?;
            incident
        }
        Some(existing) => {
            let incident = CollectorIncident {
                payload: carry_delivery_markers(&payload, &existing.payload),
                ..existing
            };
            transaction.execute(
                "UPDATE collector_incident SET payload = ? WHERE incident_key = ?",
                params![dump(&incident.payload)?, incident_key],
            )?;
            incident
        }
    };
    transaction.commit()?;
    Ok(incident)
}

/// `_acknowledge_collector_incident_delivery_async`: compare
/// `delivery_attempt` and strip both markers in one write transaction.
/// `between` runs after the read, before the write (tests).
fn acknowledge_delivery(
    connection: &mut Connection,
    incident_key: &str,
    attempt: i64,
    between: impl FnOnce(),
) -> Result<bool, StoreError> {
    let transaction = immediate(connection)?;
    let existing = select_incident(&transaction, incident_key)?;
    let matches = existing.as_ref().is_some_and(|incident| {
        // `payload.get(DELIVERY_ATTEMPT_KEY) != attempt` with Python `==`.
        incident
            .payload
            .get(DELIVERY_ATTEMPT_KEY)
            .is_some_and(|stored| py_cmp(CmpOp::Eq, stored, &PyJson::Int(attempt)).unwrap_or(false))
    });
    let Some(existing) = existing.filter(|_| matches) else {
        transaction.commit()?;
        return Ok(false);
    };
    between();
    let payload = without_delivery_markers(&existing.payload);
    transaction.execute(
        "UPDATE collector_incident SET payload = ? WHERE incident_key = ?",
        params![dump(&payload)?, incident_key],
    )?;
    transaction.commit()?;
    Ok(true)
}

/// `_set_collector_incident_payload_async`: replace the payload verbatim.
fn set_payload(
    connection: &mut Connection,
    incident_key: &str,
    payload: PyDict,
) -> Result<Option<CollectorIncident>, StoreError> {
    let transaction = immediate(connection)?;
    let Some(existing) = select_incident(&transaction, incident_key)? else {
        transaction.commit()?;
        return Ok(None);
    };
    transaction.execute(
        "UPDATE collector_incident SET payload = ? WHERE incident_key = ?",
        params![dump(&payload)?, incident_key],
    )?;
    transaction.commit()?;
    Ok(Some(CollectorIncident {
        payload,
        ..existing
    }))
}

/// `_close_collector_incident_async`: delete, returning the old row.
fn close_incident(
    connection: &mut Connection,
    incident_key: &str,
) -> Result<Option<CollectorIncident>, StoreError> {
    let transaction = immediate(connection)?;
    let existing = select_incident(&transaction, incident_key)?;
    if existing.is_some() {
        transaction.execute(
            "DELETE FROM collector_incident WHERE incident_key = ?",
            [incident_key],
        )?;
    }
    transaction.commit()?;
    Ok(existing)
}

impl Store {
    /// `get_collector_incident`.
    pub async fn get_collector_incident(
        &self,
        incident_key: &str,
    ) -> Result<Option<CollectorIncident>, StoreError> {
        let key = incident_key.to_owned();
        self.call(move |connection| select_incident(connection, &key))
            .await
    }

    /// `claim_collector_incident_alert`: a new row alerts immediately; an
    /// existing one when `now - last_alert_at >= max(1, reminder_seconds)`.
    /// `payload = None` keeps the stored payload.
    pub async fn claim_collector_incident_alert(
        &self,
        incident_key: &str,
        now: i64,
        reminder_seconds: i64,
        payload: Option<PyDict>,
    ) -> Result<CollectorIncidentAlert, StoreError> {
        let key = incident_key.to_owned();
        self.call(move |connection| claim_alert(connection, &key, now, reminder_seconds, payload))
            .await
    }

    /// `open_collector_incident`.
    pub async fn open_collector_incident(
        &self,
        incident_key: &str,
        now: i64,
        payload: PyDict,
    ) -> Result<CollectorIncident, StoreError> {
        let key = incident_key.to_owned();
        self.call(move |connection| open_incident(connection, &key, now, payload))
            .await
    }

    /// `acknowledge_collector_incident_delivery`.
    pub async fn acknowledge_collector_incident_delivery(
        &self,
        incident_key: &str,
        attempt: i64,
    ) -> Result<bool, StoreError> {
        let key = incident_key.to_owned();
        self.call(move |connection| acknowledge_delivery(connection, &key, attempt, || {}))
            .await
    }

    /// `set_collector_incident_payload`; `None` when no row exists.
    pub async fn set_collector_incident_payload(
        &self,
        incident_key: &str,
        payload: PyDict,
    ) -> Result<Option<CollectorIncident>, StoreError> {
        let key = incident_key.to_owned();
        self.call(move |connection| set_payload(connection, &key, payload))
            .await
    }

    /// `close_collector_incident`.
    pub async fn close_collector_incident(
        &self,
        incident_key: &str,
    ) -> Result<Option<CollectorIncident>, StoreError> {
        let key = incident_key.to_owned();
        self.call(move |connection| close_incident(connection, &key))
            .await
    }
}

#[cfg(test)]
mod tests {
    use std::sync::mpsc;
    use std::time::Duration;

    use super::*;

    fn dict(pairs: &[(&str, PyJson)]) -> PyDict {
        pairs
            .iter()
            .map(|(key, value)| ((*key).to_owned(), value.clone()))
            .collect()
    }

    /// `test_sqlite_ack_cannot_lose_a_claim_granted_while_it_runs`: a claim
    /// on another connection cannot run between the acknowledgement's read
    /// and its write.
    #[test]
    fn acknowledgement_cannot_lose_a_claim_granted_while_it_runs() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("db.sqlite3");
        let mut first = Connection::open(&path).unwrap();
        first
            .execute_batch(
                "CREATE TABLE collector_incident (incident_key TEXT PRIMARY KEY,
                    opened_at INTEGER NOT NULL DEFAULT 0, last_alert_at INTEGER NOT NULL DEFAULT 0,
                    alert_count INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL DEFAULT '{}')",
            )
            .unwrap();
        let key = "collector:stale_processing_lease";
        claim_alert(
            &mut first,
            key,
            1_000,
            3_600,
            Some(dict(&[("reclaimed_count", PyJson::Int(3))])),
        )
        .unwrap();

        let (done_tx, done_rx) = mpsc::channel();
        let competitor_path = path.clone();
        let mut raced = None;
        let mut competitor = None;
        let acknowledged = acknowledge_delivery(&mut first, key, 1, || {
            competitor = Some(std::thread::spawn(move || {
                let mut second = Connection::open(&competitor_path).unwrap();
                second.busy_timeout(Duration::from_secs(10)).unwrap();
                let payload = dict(&[("reclaimed_count", PyJson::Int(9))]);
                claim_alert(&mut second, key, 5_000, 3_600, Some(payload)).unwrap();
                done_tx.send(()).unwrap();
            }));
            raced = Some(done_rx.recv_timeout(Duration::from_millis(300)).is_ok());
        })
        .unwrap();
        competitor.unwrap().join().unwrap();

        assert!(acknowledged);
        assert_eq!(
            raced,
            Some(false),
            "the claim ran inside the acknowledgement's write"
        );
        let incident = select_incident(&first, key).unwrap().unwrap();
        assert_eq!(incident.alert_count, 2);
        assert_eq!(
            dump(&incident.payload).unwrap(),
            r#"{"reclaimed_count": 9, "delivery_pending": true, "delivery_attempt": 2}"#
        );
    }
}
