//! Log output in the format of `cli.py:setup_logging` (PLAN.md 1.7):
//! `%(asctime)s - %(name)s - %(levelname)s - %(message)s` on stderr, with
//! asctime in local time as `YYYY-MM-DD HH:MM:SS,mmm` and the Python logger
//! name as the `tracing` target.
//!
//! A small `tracing` subscriber instead of `tracing-subscriber`: the format
//! is fixed, spans are not used, and tests capture the lines in memory.

use std::fmt::{self, Write as _};
use std::io::Write as _;
use std::sync::{Arc, Mutex};

use tracing::field::{Field, Visit};
use tracing::level_filters::LevelFilter;
use tracing::subscriber::Interest;
use tracing::{Event, Level, Metadata, span};

/// Install the binary's payload-free panic hook and stderr subscriber.
pub fn install_logging(log_level: &str) -> bool {
    // Install once at command startup, never around individual tasks. Do not
    // invoke the default hook: it prints payloads and possibly a backtrace.
    std::panic::set_hook(Box::new(|_| {
        let _ = writeln!(
            std::io::stderr().lock(),
            "nyxmon-agent: panic (payload withheld)"
        );
    }));
    let level = level_filter(log_level).expect("restricted by clap");
    if tracing::subscriber::set_global_default(PyLogSubscriber::stderr(level)).is_err() {
        eprintln!("cannot install the log subscriber");
        return false;
    }
    true
}

/// `--log-level` choices.
pub const LOG_LEVELS: [&str; 5] = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];

/// The filter of a Python level name (`CRITICAL`: nothing the agent logs).
pub fn level_filter(name: &str) -> Option<LevelFilter> {
    Some(match name {
        "DEBUG" => LevelFilter::DEBUG,
        "INFO" => LevelFilter::INFO,
        "WARNING" => LevelFilter::WARN,
        "ERROR" => LevelFilter::ERROR,
        "CRITICAL" => LevelFilter::OFF,
        _ => return None,
    })
}

/// The Python `levelname` of a `tracing` level.
pub fn level_name(level: &Level) -> &'static str {
    match *level {
        Level::ERROR => "ERROR",
        Level::WARN => "WARNING",
        Level::INFO => "INFO",
        _ => "DEBUG",
    }
}

/// One captured line (tests).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LogLine {
    pub level: &'static str,
    pub logger: String,
    pub message: String,
}

#[derive(Clone)]
enum Sink {
    Stderr,
    Memory(Arc<Mutex<Vec<LogLine>>>),
}

/// The subscriber.
#[derive(Clone)]
pub struct PyLogSubscriber {
    max_level: LevelFilter,
    sink: Sink,
}

impl PyLogSubscriber {
    /// Lines on stderr up to `max_level`.
    pub fn stderr(max_level: LevelFilter) -> Self {
        PyLogSubscriber {
            max_level,
            sink: Sink::Stderr,
        }
    }

    /// Lines captured in memory (tests), every level.
    pub fn memory() -> (Self, LogCapture) {
        let lines = Arc::new(Mutex::new(Vec::new()));
        (
            PyLogSubscriber {
                max_level: LevelFilter::TRACE,
                sink: Sink::Memory(lines.clone()),
            },
            LogCapture { lines },
        )
    }
}

/// Handle on the captured lines.
#[derive(Clone, Debug)]
pub struct LogCapture {
    lines: Arc<Mutex<Vec<LogLine>>>,
}

impl LogCapture {
    pub fn lines(&self) -> Vec<LogLine> {
        self.lines.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }

    /// The messages of the captured lines.
    pub fn messages(&self) -> Vec<String> {
        self.lines().into_iter().map(|line| line.message).collect()
    }

    /// Whether any message contains `text` (`text in caplog.text`).
    pub fn contains(&self, text: &str) -> bool {
        self.lines().iter().any(|line| line.message.contains(text))
    }
}

/// HTTP/TLS libraries log at WARNING and above only (`setup_logging`
/// silences httpx/httpcore; PLAN.md 5.1).
fn is_library_target(target: &str) -> bool {
    !target.starts_with("nyxmon")
}

const MAX_RECORD_BYTES: usize = 8192;
const TRUNCATED: &str = "[truncated]";

#[derive(Default)]
struct BoundedText(String);

impl fmt::Write for BoundedText {
    fn write_str(&mut self, text: &str) -> fmt::Result {
        let remaining = MAX_RECORD_BYTES.saturating_sub(self.0.len());
        if text.len() <= remaining {
            self.0.push_str(text);
            return Ok(());
        }
        let mut end = remaining;
        while !text.is_char_boundary(end) {
            end -= 1;
        }
        self.0.push_str(&text[..end]);
        Err(fmt::Error)
    }
}

// All dynamic record components pass this final stderr boundary. Reserve the
// marker before appending, never split UTF-8 or an escaped control sequence.
fn stderr_record(line: &LogLine) -> String {
    let asctime = chrono::Local::now().format("%Y-%m-%d %H:%M:%S,%3f");
    let mut record = format!("{asctime} - ");
    for text in [&line.logger, " - ", line.level, " - ", &line.message] {
        for ch in text.chars() {
            let escaped = match ch {
                '\n' => "\\n".to_owned(),
                '\r' => "\\r".to_owned(),
                '\t' => "\\t".to_owned(),
                c if c.is_control()
                    || matches!(c, '\u{2028}' | '\u{2029}' | '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}') =>
                {
                    format!("\\u{{{:x}}}", u32::from(c))
                }
                c => c.to_string(),
            };
            if record.len() + escaped.len() > MAX_RECORD_BYTES - TRUNCATED.len() {
                record.push_str(TRUNCATED);
                return record;
            }
            record.push_str(&escaped);
        }
    }
    record
}

#[derive(Default)]
struct MessageVisitor {
    message: BoundedText,
    fields: BoundedText,
}

impl Visit for MessageVisitor {
    fn record_str(&mut self, field: &Field, value: &str) {
        if field.name() == "message" {
            let _ = self.message.write_str(value);
        } else {
            let _ = write!(self.fields, " {}={value}", field.name());
        }
    }

    fn record_debug(&mut self, field: &Field, value: &dyn fmt::Debug) {
        if field.name() == "message" {
            let _ = write!(self.message, "{value:?}");
        } else {
            let _ = write!(self.fields, " {}={value:?}", field.name());
        }
    }
}

impl tracing::Subscriber for PyLogSubscriber {
    fn register_callsite(&self, _metadata: &'static Metadata<'static>) -> Interest {
        // Evaluate `enabled` per event: tests install several subscribers.
        Interest::sometimes()
    }

    fn enabled(&self, metadata: &Metadata<'_>) -> bool {
        let level = metadata.level();
        if is_library_target(metadata.target()) && *level > Level::WARN {
            return false;
        }
        self.max_level >= *level
    }

    fn max_level_hint(&self) -> Option<LevelFilter> {
        Some(self.max_level)
    }

    fn new_span(&self, _span: &span::Attributes<'_>) -> span::Id {
        span::Id::from_u64(1)
    }

    fn record(&self, _span: &span::Id, _values: &span::Record<'_>) {}

    fn record_follows_from(&self, _span: &span::Id, _follows: &span::Id) {}

    fn event(&self, event: &Event<'_>) {
        let mut visitor = MessageVisitor::default();
        event.record(&mut visitor);
        let metadata = event.metadata();
        let line = LogLine {
            level: level_name(metadata.level()),
            logger: metadata.target().to_owned(),
            message: visitor.message.0 + &visitor.fields.0,
        };
        match &self.sink {
            Sink::Stderr => {
                let _ = writeln!(std::io::stderr().lock(), "{}", stderr_record(&line));
            }
            Sink::Memory(lines) => lines.lock().unwrap_or_else(|e| e.into_inner()).push(line),
        }
    }

    fn enter(&self, _span: &span::Id) {}

    fn exit(&self, _span: &span::Id) {}
}

/// Capture the log lines of the current thread until the guard is dropped
/// (tests on a current-thread runtime).
pub fn capture() -> (LogCapture, tracing::subscriber::DefaultGuard) {
    let (subscriber, capture) = PyLogSubscriber::memory();
    (capture, tracing::subscriber::set_default(subscriber))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stderr_records_are_bounded_and_cannot_forge_lines() {
        if std::env::var_os("NYXMON_LOG_FRAMING_CHILD").is_some() {
            let subscriber = PyLogSubscriber::stderr(LevelFilter::INFO);
            let _guard = tracing::subscriber::set_default(subscriber);
            let mut check = crate::testutil::check(1);
            check.name = "café\nFORGED\r\x1b[31m\u{2028}".to_owned();
            check.check_type = "unknown".to_owned();
            crate::startup::validate_check_types(&[check], &[]);
            tracing::error!(target: "nyxmon.test", error = "bad\nfield\r\x00", "{}", "ü".repeat(20000));
            tracing::error!(target: "nyxmon.test", error = "bad\nfield\r\x00", "short");
            return;
        }
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "logging::tests::stderr_records_are_bounded_and_cannot_forge_lines",
                "--nocapture",
            ])
            .env("NYXMON_LOG_FRAMING_CHILD", "1")
            .output()
            .unwrap();
        assert!(output.status.success());
        let text = String::from_utf8(output.stderr).unwrap();
        let lines: Vec<_> = text.lines().collect();
        assert_eq!(lines.len(), 6, "{text}");
        assert!(lines.iter().all(|line| line.len() <= 8192));
        assert!(text.contains("café\\nFORGED\\r\\u{1b}[31m\\u{2028}"));
        assert!(text.contains("bad\\nfield\\r\\u{0}"));
        assert!(!text.contains('\x1b'));
        assert!(text.contains("[truncated]"));
    }

    #[test]
    fn levels_map_to_python_names() {
        assert_eq!(level_filter("WARNING"), Some(LevelFilter::WARN));
        assert_eq!(level_filter("CRITICAL"), Some(LevelFilter::OFF));
        assert_eq!(level_filter("warning"), None);
        assert_eq!(level_name(&Level::WARN), "WARNING");
        assert_eq!(level_name(&Level::TRACE), "DEBUG");
    }

    #[test]
    fn events_are_captured_with_the_logger_name() {
        let (capture, _guard) = capture();
        tracing::warn!(target: "nyxmon.adapters.collector", "lease {} is short", 30);
        tracing::info!(target: "hyper::proto", "library chatter");
        tracing::warn!(target: "hyper::proto", "library warning");
        assert_eq!(
            capture.lines(),
            vec![
                LogLine {
                    level: "WARNING",
                    logger: "nyxmon.adapters.collector".to_owned(),
                    message: "lease 30 is short".to_owned(),
                },
                LogLine {
                    level: "WARNING",
                    logger: "hyper::proto".to_owned(),
                    message: "library warning".to_owned(),
                },
            ]
        );
    }

    #[test]
    fn the_level_filter_applies() {
        let subscriber = PyLogSubscriber::stderr(LevelFilter::WARN);
        let _guard = tracing::subscriber::set_default(subscriber);
        assert!(!tracing::enabled!(target: "nyxmon.x", Level::INFO));
        assert!(tracing::enabled!(target: "nyxmon.x", Level::WARN));
    }
}
