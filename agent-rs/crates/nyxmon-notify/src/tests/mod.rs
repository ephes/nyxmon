//! Notifier tests: the pure parts against `fixtures/telegram.json` and
//! `fixtures/opsgate.json`, whole notify calls against a local fake
//! Telegram/OpsGate server, replaying the HTTP scenarios the Python notifier
//! ran against `httpx.MockTransport`.

mod fake_server;
mod fixtures;
mod http;

use nyxmon_core::model::{Check, CheckResult, ResultStatus};
use nyxmon_core::pyjson::{PyJson, loads};

pub(crate) fn load(text: &str) -> PyJson {
    loads(text).expect("fixture file is valid JSON")
}

pub(crate) fn field<'a>(value: &'a PyJson, key: &str) -> &'a PyJson {
    value
        .get(key)
        .unwrap_or_else(|| panic!("fixture field {key:?} missing"))
}

pub(crate) fn str_field<'a>(value: &'a PyJson, key: &str) -> &'a str {
    field(value, key)
        .as_str()
        .unwrap_or_else(|| panic!("fixture field {key:?} is not a string"))
}

pub(crate) fn int_field(value: &PyJson, key: &str) -> i64 {
    field(value, key)
        .as_i64()
        .unwrap_or_else(|| panic!("fixture field {key:?} is not an int"))
}

pub(crate) fn list_field<'a>(value: &'a PyJson, key: &str) -> &'a [PyJson] {
    field(value, key)
        .as_list()
        .unwrap_or_else(|| panic!("fixture field {key:?} is not a list"))
}

/// A `Check` from a fixture spec (`check_id`, `service_id`, `name`,
/// `check_type`, `url`).
pub(crate) fn check_of(spec: &PyJson) -> Check {
    Check {
        check_id: int_field(spec, "check_id"),
        service_id: int_field(spec, "service_id"),
        name: str_field(spec, "name").to_owned(),
        check_type: str_field(spec, "check_type").to_owned(),
        url: str_field(spec, "url").to_owned(),
        check_interval: 300,
        next_check_time: 0,
        processing_started_at: 0,
        status: "idle".to_owned(),
        disabled: false,
        data: PyJson::dict(),
        claim_started_at: 0,
    }
}

/// A `CheckResult` from a fixture spec (`check_id`, `status`, `data` as JSON
/// text).
pub(crate) fn result_of(spec: &PyJson) -> CheckResult {
    let data = loads(str_field(spec, "data"))
        .expect("result data is JSON")
        .into_dict()
        .expect("result data is a dict");
    CheckResult::new(
        int_field(spec, "check_id"),
        ResultStatus::parse(str_field(spec, "status")).expect("known status"),
        data,
    )
}
