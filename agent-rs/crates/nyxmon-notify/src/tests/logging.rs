//! Handwritten diagnostics contract, independent of legacy text fixtures.
use std::sync::Arc;

use nyxmon_core::model::{CheckResult, ResultStatus};
use nyxmon_core::pyjson::{PyDict, PyJson};

use super::{check_of, load};
use crate::log::{Level, RecordingLog};
use crate::logging::LoggingNotifier;
use crate::{Delivery, Notifier};

#[tokio::test]
async fn hostile_labels_remain_one_bounded_delivered_line() {
    for label in [
        "name\r\nFORGED\u{1b}[31m\u{2028}\u{2029}\u{85}\u{200e}é漢".to_owned(),
        "界".repeat(5000),
    ] {
        for shadow in [false, true] {
            let mut check = check_of(&load(
                r#"{"check_id":11,"service_id":1,"name":"ordinary","check_type":"http","url":"https://example.invalid"}"#,
            ));
            check.name = label.clone();
            let mut data = PyDict::new();
            data.insert("error_type".to_owned(), PyJson::from(label.clone()));
            data.insert("detail".to_owned(), PyJson::from("x".repeat(10_000)));
            let result = CheckResult::new(11, ResultStatus::Warning, data);
            let log = Arc::new(RecordingLog::default());
            let notifier = LoggingNotifier::with_log(shadow, log.clone());
            assert_eq!(
                notifier.notify_check_failed(&check, &result).await,
                Ok(Delivery::Delivered)
            );
            let lines = log.take();
            assert_eq!(lines.len(), 1);
            assert_eq!(lines[0].0, Level::Error);
            let text = &lines[0].1;
            assert_eq!(text.lines().count(), 1);
            assert!(
                !text
                    .chars()
                    .any(|c| c.is_control() || matches!(c, '\u{2028}' | '\u{2029}' | '\u{200e}'))
            );
            assert!(
                text.chars().count() < 1500,
                "{} characters",
                text.chars().count()
            );
            if shadow {
                assert!(text.starts_with("notification check_id=11 status=warning error_type="));
            } else {
                assert!(text.contains(
                    "(ID: 11), Result: Result(result_id=None, check_id=11 status=warning"
                ));
            }
            if label.starts_with('界') {
                assert!(text.contains(&"界".repeat(200)));
                let label_text = if shadow {
                    text.as_str()
                } else {
                    text.split(" (ID:").next().unwrap()
                };
                assert!(!label_text.contains(&"界".repeat(201)));
                assert!(text.contains("...[truncated 4800 chars]"));
            } else {
                assert!(text.contains("name??FORGED?[31m????é漢"));
            }
        }
    }
}
