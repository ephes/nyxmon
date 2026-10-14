//! The synthetic check/result pair of collector-scoped alerts
//! (`adapters/collector.py:build_incident_notification`, `_result_status`;
//! PLAN.md 5.8, 6.11).
//!
//! The check row exists only to reuse the notifier's message and ticket
//! shape; it is never persisted. `incident_key` gives the alert a stable
//! identity so downstream ticketing deduplicates it too.

use nyxmon_core::PyJson;
use nyxmon_core::model::{Check, CheckResult, ResultStatus};
use nyxmon_core::pyjson::PyDict;

/// `_result_status`: `ok` and `warning` map to themselves, anything else is
/// an error.
pub fn result_status(status: &str) -> ResultStatus {
    match status {
        "ok" => ResultStatus::Ok,
        "warning" => ResultStatus::Warning,
        _ => ResultStatus::Error,
    }
}

/// The arguments of `build_incident_notification`.
#[derive(Clone, Debug)]
pub struct IncidentNotification<'a> {
    pub incident_key: &'a str,
    pub name: &'a str,
    pub error_type: &'a str,
    pub error_msg: &'a str,
    pub status: ResultStatus,
    /// `false` asks the notifier to open no remediation ticket.
    pub opsgate_ticket: bool,
    pub alert_count: i64,
}

/// `build_incident_notification`: `check_id=0`, `service_id=0`,
/// `check_type="internal"`, `url="internal://collector"`, the `Check`
/// defaults otherwise; result data `error_type, error_msg, incident_key,
/// incident_alert_count[, opsgate_ticket=false]` in that order.
pub fn build_incident_notification(incident: &IncidentNotification<'_>) -> (Check, CheckResult) {
    let check = Check {
        check_id: 0,
        service_id: 0,
        name: incident.name.to_owned(),
        check_type: "internal".to_owned(),
        url: "internal://collector".to_owned(),
        check_interval: 300,
        next_check_time: 0,
        processing_started_at: 0,
        status: "idle".to_owned(),
        disabled: false,
        data: PyJson::dict(),
        claim_started_at: 0,
    };
    let mut data = PyDict::new();
    data.insert(
        "error_type".to_owned(),
        PyJson::Str(incident.error_type.to_owned()),
    );
    data.insert(
        "error_msg".to_owned(),
        PyJson::Str(incident.error_msg.to_owned()),
    );
    data.insert(
        "incident_key".to_owned(),
        PyJson::Str(incident.incident_key.to_owned()),
    );
    data.insert(
        "incident_alert_count".to_owned(),
        PyJson::Int(incident.alert_count),
    );
    if !incident.opsgate_ticket {
        data.insert("opsgate_ticket".to_owned(), PyJson::Bool(false));
    }
    (check, CheckResult::new(0, incident.status, data))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_synthetic_pair_matches_python() {
        let (check, result) = build_incident_notification(&IncidentNotification {
            incident_key: "site:connectivity",
            name: "site connectivity",
            error_type: "site_connectivity_summary",
            error_msg: "over",
            status: result_status("warning"),
            opsgate_ticket: false,
            alert_count: 0,
        });
        assert_eq!(
            (check.check_id, check.service_id, check.check_interval),
            (0, 0, 300)
        );
        assert_eq!(check.check_type, "internal");
        assert_eq!(check.url, "internal://collector");
        assert_eq!(check.name, "site connectivity");
        assert_eq!(result.check_id, 0);
        assert_eq!(result.status, ResultStatus::Warning);
        assert_eq!(
            PyJson::Dict(result.data).to_json_string().unwrap(),
            r#"{"error_type": "site_connectivity_summary", "error_msg": "over", "incident_key": "site:connectivity", "incident_alert_count": 0, "opsgate_ticket": false}"#
        );

        let (_, result) = build_incident_notification(&IncidentNotification {
            incident_key: "collector:x",
            name: "n",
            error_type: "t",
            error_msg: "m",
            status: result_status("error"),
            opsgate_ticket: true,
            alert_count: 3,
        });
        assert_eq!(result.status, ResultStatus::Error);
        assert!(!result.data.contains_key("opsgate_ticket"));
        assert_eq!(result_status("ok"), ResultStatus::Ok);
        assert_eq!(result_status("bogus"), ResultStatus::Error);
    }
}
