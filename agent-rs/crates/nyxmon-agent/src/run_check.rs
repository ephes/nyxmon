//! `nyxmon-agent run-check` (PLAN.md 7.4, 9.1 phase C): execute one check
//! from a database opened read-only and print the result as JSON.
//!
//! No persistence, no notification, no agent lock: the check runs through
//! the same registry and runner as a claimed batch of one (unknown types,
//! undecodable data, executor exceptions and panics give the runner results
//! of PLAN.md 1.6), and the database is never written. Disabled checks run
//! too, because the shadow copy disables the mail checks that phase C runs.

use std::path::Path;
use std::process::ExitCode;
use std::sync::Arc;

use clap::Parser;
use nyxmon_checks::registry::{Executors, Registry};
use nyxmon_core::model::{Check, CheckResult};
use nyxmon_core::pyjson::{PyDict, PyJson};
use nyxmon_store::{check_schema, read_check};

use crate::cli::{VERSION, py_path_str};
use crate::logging::LOG_LEVELS;
use crate::runner::run_batch;

macro_rules! run_check_error {
    ($($arg:tt)*) => { tracing::error!(target: "nyxmon.entrypoints.cli", $($arg)*) };
}

/// Execute one check and print its result (no persistence, no
/// notification, no lock)
#[derive(Debug, Parser)]
#[command(name = "nyxmon-agent run-check", version = VERSION)]
pub struct RunCheckArgs {
    /// Path to SQLite database file (opened read-only)
    #[arg(long)]
    pub db: String,

    /// `health_check.id` of the check to execute
    #[arg(long)]
    pub check_id: i64,

    /// Run an IMAP check with `delete_after_check` forced to false, so the
    /// matched messages stay in the mailbox
    #[arg(long)]
    pub imap_no_delete: bool,

    /// Set the logging level (default: INFO)
    #[arg(long, default_value = "INFO", value_parser = LOG_LEVELS)]
    pub log_level: String,
}

/// `delete_after_check: false` for an IMAP check whose data is an object;
/// other checks and non-object data are returned unchanged.
pub fn without_imap_delete(mut check: Check) -> Check {
    if check.check_type == "imap"
        && let Some(data) = check.data.as_dict_mut()
    {
        data.insert("delete_after_check".to_owned(), PyJson::Bool(false));
    }
    check
}

/// The printed document: `{"check_id", "check_type", "status", "data"}`,
/// serialised like `json.dumps` (the data keys in result order).
pub fn result_document(check: &Check, result: &CheckResult) -> PyJson {
    let mut document = PyDict::new();
    document.insert("check_id".to_owned(), PyJson::Int(result.check_id));
    document.insert(
        "check_type".to_owned(),
        PyJson::from(check.check_type.as_str()),
    );
    document.insert("status".to_owned(), PyJson::from(result.status.as_str()));
    document.insert("data".to_owned(), PyJson::Dict(result.data.clone()));
    PyJson::Dict(document)
}

/// Execute `check` through `executors` like a claimed batch of one.
pub async fn execute(executors: Arc<dyn Executors>, check: Check) -> CheckResult {
    let mut outcome = None;
    run_batch(&executors, vec![check], None, |_, result| {
        outcome = Some(result);
        async {}
    })
    .await;
    outcome.expect("a batch without intake stop yields every result")
}

/// The `run-check` command after argument parsing and logging setup.
pub fn run_check(args: &RunCheckArgs) -> ExitCode {
    let db_path = Path::new(&args.db);
    if !db_path.exists() {
        run_check_error!("Database file not found: {}", py_path_str(&args.db));
        return ExitCode::FAILURE;
    }
    match check_schema(db_path) {
        Ok(report) if report.is_ok() => {}
        Ok(report) => {
            run_check_error!("{report}");
            return ExitCode::FAILURE;
        }
        Err(err) => {
            run_check_error!("cannot verify the database schema: {err}");
            return ExitCode::FAILURE;
        }
    }
    let check = match read_check(db_path, args.check_id) {
        Ok(Some(check)) => check,
        Ok(None) => {
            run_check_error!("check {} not found in {}", args.check_id, args.db);
            return ExitCode::FAILURE;
        }
        Err(err) => {
            run_check_error!("cannot read check {}: {err}", args.check_id);
            return ExitCode::FAILURE;
        }
    };
    let check = if args.imap_no_delete {
        without_imap_delete(check)
    } else {
        check
    };
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .max_blocking_threads(32)
        .enable_all()
        .build()
    {
        Ok(runtime) => runtime,
        Err(err) => {
            run_check_error!("cannot start the runtime: {err}");
            return ExitCode::FAILURE;
        }
    };
    let executors: Arc<dyn Executors> = Arc::new(Registry::new());
    let result = runtime.block_on(execute(executors, check.clone()));
    runtime.shutdown_background();
    match result_document(&check, &result).to_json_string() {
        Ok(text) => {
            println!("{text}");
            ExitCode::SUCCESS
        }
        Err(err) => {
            run_check_error!("cannot serialise the result: {err:?}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use nyxmon_core::model::ResultStatus;

    use super::*;

    fn check(check_type: &str, data: &str) -> Check {
        Check {
            check_id: 7,
            service_id: 1,
            name: "c".to_owned(),
            check_type: check_type.to_owned(),
            url: String::new(),
            check_interval: 60,
            next_check_time: 0,
            processing_started_at: 0,
            status: "idle".to_owned(),
            disabled: false,
            data: nyxmon_core::pyjson::loads(data).unwrap(),
            claim_started_at: 0,
        }
    }

    #[test]
    fn imap_no_delete_overrides_only_imap_objects() {
        let imap = without_imap_delete(check("imap", r#"{"delete_after_check": true, "a": 1}"#));
        assert_eq!(
            imap.data.to_json_string().unwrap(),
            r#"{"delete_after_check": false, "a": 1}"#
        );
        let added = without_imap_delete(check("imap", r#"{"host": "h"}"#));
        assert_eq!(
            added.data.to_json_string().unwrap(),
            r#"{"host": "h", "delete_after_check": false}"#
        );
        for (check_type, data) in [("smtp", r#"{"delete_after_check": true}"#), ("imap", "[1]")] {
            let unchanged = without_imap_delete(check(check_type, data));
            assert_eq!(unchanged.data.to_json_string().unwrap(), data);
        }
    }

    #[test]
    fn the_document_keeps_the_result_key_order() {
        let mut data = PyDict::new();
        data.insert("z".to_owned(), PyJson::Int(1));
        data.insert("a".to_owned(), PyJson::from("x"));
        let result = CheckResult::new(7, ResultStatus::Warning, data);
        assert_eq!(
            result_document(&check("http", "{}"), &result)
                .to_json_string()
                .unwrap(),
            r#"{"check_id": 7, "check_type": "http", "status": "warning", "data": {"z": 1, "a": "x"}}"#
        );
    }

    #[tokio::test]
    async fn an_unknown_type_gives_the_runner_result() {
        let result = execute(Arc::new(Registry::new()), check("legacy", "{}")).await;
        assert_eq!(result.status, ResultStatus::Error);
        assert_eq!(
            result.data.get("error_type"),
            Some(&PyJson::from("unknown_check_type"))
        );
    }
}
