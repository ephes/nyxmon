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

#[derive(Default)]
struct MessageVisitor {
    message: String,
    fields: String,
}

impl Visit for MessageVisitor {
    fn record_str(&mut self, field: &Field, value: &str) {
        if field.name() == "message" {
            self.message.push_str(value);
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
            message: visitor.message + &visitor.fields,
        };
        match &self.sink {
            Sink::Stderr => {
                let asctime = chrono::Local::now().format("%Y-%m-%d %H:%M:%S,%3f");
                let mut stderr = std::io::stderr().lock();
                let _ = writeln!(
                    stderr,
                    "{asctime} - {} - {} - {}",
                    line.logger, line.level, line.message
                );
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
