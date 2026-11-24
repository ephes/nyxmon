//! Log output in the format of `cli.py:setup_logging` (PLAN.md 1.7):
//! `%(asctime)s - %(name)s - %(levelname)s - %(message)s` on stderr, with
//! asctime in local time as `YYYY-MM-DD HH:MM:SS,mmm` and the Python logger
//! name as the `tracing` target.
//!
//! A small `tracing` subscriber instead of `tracing-subscriber`: the format
//! is fixed, spans are not used, and tests capture the lines in memory.

use std::fmt::{self, Write as _};
use std::io;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock, mpsc};
use std::time::Duration;

use tracing::field::{Field, Visit};
use tracing::level_filters::LevelFilter;
use tracing::subscriber::Interest;
use tracing::{Event, Level, Metadata, span};

// Fixed-size records and a fixed-capacity std channel bound retained text to
// 128 * 8193 bytes, plus the record currently held by the writer. No sink I/O
// or sink lock is ever taken by a submitting runtime thread.
const STDERR_QUEUE_RECORDS: usize = 128;
const STDERR_SHUTDOWN_WAIT: Duration = Duration::from_millis(250);

struct LogQueue {
    sender: mpsc::SyncSender<Box<str>>,
    finished: mpsc::Receiver<()>,
}

struct StderrWriter {
    queue: Mutex<Option<LogQueue>>,
    dropped: Arc<AtomicU64>,
}

impl StderrWriter {
    fn start() -> io::Result<Self> {
        Self::start_with(std::thread::Builder::spawn)
    }

    fn start_with(
        spawn: impl FnOnce(
            std::thread::Builder,
            Box<dyn FnOnce() + Send>,
        ) -> io::Result<std::thread::JoinHandle<()>>,
    ) -> io::Result<Self> {
        Self::start_with_sink(spawn, std::io::stderr())
    }

    fn start_with_sink(
        spawn: impl FnOnce(
            std::thread::Builder,
            Box<dyn FnOnce() + Send>,
        ) -> io::Result<std::thread::JoinHandle<()>>,
        mut sink: impl io::Write + Send + 'static,
    ) -> io::Result<Self> {
        let (sender, receiver) = mpsc::sync_channel::<Box<str>>(STDERR_QUEUE_RECORDS);
        let (done, finished) = mpsc::channel();
        let dropped = Arc::new(AtomicU64::new(0));
        let failed = Arc::clone(&dropped);
        // A plain thread, deliberately outside Tokio's blocking pool. Never
        // join it: a supervisor may stop draining stderr indefinitely.
        spawn(
            std::thread::Builder::new().name("nyxmon-stderr".to_owned()),
            Box::new(move || {
                while let Ok(record) = receiver.recv() {
                    if sink.write_all(record.as_bytes()).is_err() {
                        failed.fetch_add(1, Ordering::Relaxed);
                    }
                }
                let _ = sink.flush();
                let _ = done.send(());
            }),
        )?;
        Ok(Self {
            queue: Mutex::new(Some(LogQueue { sender, finished })),
            dropped,
        })
    }

    fn submit(&self, mut record: String) {
        record.push('\n');
        let record = record.into_boxed_str();
        // Serialize admission with shutdown. The writer never takes this lock.
        let accepted = self
            .queue
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .as_ref()
            .is_some_and(|queue| queue.sender.try_send(record).is_ok());
        if !accepted {
            // Drop newest on full/closed/disconnected; never recursively log loss.
            self.dropped.fetch_add(1, Ordering::Relaxed);
        }
    }

    fn finish(&self) {
        let queue = self.queue.lock().unwrap_or_else(|e| e.into_inner()).take();
        if let Some(LogQueue { sender, finished }) = queue {
            // Disconnect admission so recv drains all records before returning an error.
            drop(sender);
            let _ = finished.recv_timeout(STDERR_SHUTDOWN_WAIT);
        }
    }
}

static STDERR_WRITER: OnceLock<io::Result<StderrWriter>> = OnceLock::new();

fn stderr_writer() -> Result<&'static StderrWriter, &'static io::Error> {
    STDERR_WRITER.get_or_init(StderrWriter::start).as_ref()
}

/// Number of records rejected by the queue or lost to a sink write error.
/// Reading this counter performs no I/O and cannot recursively log.
pub fn dropped_log_records() -> u64 {
    STDERR_WRITER
        .get()
        .and_then(|result| result.as_ref().ok())
        .map_or(0, |writer| writer.dropped.load(Ordering::Relaxed))
}

/// Close admission and wait up to 250 ms for the ordered drain and flush.
/// Only the first request waits, and the writer is never joined.
pub fn shutdown_logging() {
    if let Some(Ok(writer)) = STDERR_WRITER.get() {
        writer.finish();
    }
}

/// Keep this guard alive until the binary exits, including during unwinding.
pub struct LoggingShutdownGuard;

impl Drop for LoggingShutdownGuard {
    fn drop(&mut self) {
        shutdown_logging();
    }
}

/// Install the binary's payload-free panic hook and stderr subscriber.
/// Return false if logging cannot be started.
pub fn install_logging(log_level: &str) -> bool {
    install_logging_with(log_level, stderr_writer())
}

fn install_logging_with(
    log_level: &str,
    writer: Result<&'static StderrWriter, &io::Error>,
) -> bool {
    let Ok(writer) = writer else {
        // A synchronous fallback could block indefinitely on an unread pipe.
        return false;
    };
    // Install once at command startup, never around individual tasks. Do not
    // invoke the default hook: it prints payloads and possibly a backtrace.
    std::panic::set_hook(Box::new(move |_| {
        writer.submit("nyxmon-agent: panic (payload withheld)".to_owned());
    }));
    let level = level_filter(log_level).expect("restricted by clap");
    if tracing::subscriber::set_global_default(PyLogSubscriber::with_stderr(level, writer)).is_err()
    {
        writer.submit("cannot install the log subscriber".to_owned());
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
    Stderr(&'static StderrWriter),
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
    /// Returns the thread-start error if the process-wide writer could not start.
    pub fn stderr(max_level: LevelFilter) -> Result<Self, &'static io::Error> {
        stderr_writer().map(|writer| Self::with_stderr(max_level, writer))
    }

    fn with_stderr(max_level: LevelFilter, writer: &'static StderrWriter) -> Self {
        PyLogSubscriber {
            max_level,
            sink: Sink::Stderr(writer),
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
            Sink::Stderr(writer) => {
                writer.submit(stderr_record(&line));
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
    fn shutdown_drains_a_full_queue_when_the_sink_recovers() {
        struct GatedSink {
            started: mpsc::Sender<()>,
            release: Option<mpsc::Receiver<()>>,
            records: mpsc::Sender<Vec<u8>>,
        }

        impl io::Write for GatedSink {
            fn write(&mut self, record: &[u8]) -> io::Result<usize> {
                if let Some(release) = self.release.take() {
                    self.started.send(()).unwrap();
                    release.recv().unwrap();
                }
                self.records.send(record.to_vec()).unwrap();
                Ok(record.len())
            }

            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }

        let (started, writing) = mpsc::channel();
        let (release, blocked) = mpsc::channel();
        let (records, written) = mpsc::channel();
        let writer = StderrWriter::start_with_sink(
            std::thread::Builder::spawn,
            GatedSink {
                started,
                release: Some(blocked),
                records,
            },
        )
        .unwrap();
        writer.submit("0".to_owned());
        writing.recv_timeout(Duration::from_secs(1)).unwrap();
        for i in 1..=STDERR_QUEUE_RECORDS {
            writer.submit(i.to_string());
        }
        assert_eq!(writer.dropped.load(Ordering::Relaxed), 0);

        let (done, finished) = mpsc::channel();
        let shutdown = std::thread::spawn(move || {
            writer.finish();
            done.send(()).unwrap();
            writer
        });
        let before_release = finished.recv_timeout(Duration::from_millis(25));
        release.send(()).unwrap();
        let writer = shutdown.join().unwrap();
        assert_eq!(before_release, Err(mpsc::RecvTimeoutError::Timeout));
        for i in 0..=STDERR_QUEUE_RECORDS {
            assert_eq!(written.try_recv().unwrap(), format!("{i}\n").as_bytes());
        }
        writer.submit("after shutdown".to_owned());
        assert_eq!(writer.dropped.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn stderr_thread_start_failure_refuses_logging_installation() {
        static WRITER: OnceLock<io::Result<StderrWriter>> = OnceLock::new();
        let result = WRITER
            .get_or_init(|| StderrWriter::start_with(|_, _| Err(io::ErrorKind::WouldBlock.into())));
        assert!(!install_logging_with("INFO", result.as_ref()));
    }

    #[test]
    fn stderr_records_are_bounded_and_cannot_forge_lines() {
        if std::env::var_os("NYXMON_LOG_FRAMING_CHILD").is_some() {
            let _shutdown = LoggingShutdownGuard;
            let subscriber = PyLogSubscriber::stderr(LevelFilter::INFO).unwrap();
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

    #[cfg(unix)]
    #[test]
    fn unread_stderr_keeps_both_workers_timers_and_sigterm_live() {
        use std::io::{BufRead, BufReader};
        use std::process::{Command, Stdio};
        use std::time::Duration;
        const CHILD: &str = "NYXMON_LOG_BACKPRESSURE_CHILD";
        if std::env::var_os(CHILD).is_some() {
            assert!(install_logging("INFO"));
            let runtime = tokio::runtime::Builder::new_multi_thread()
                .worker_threads(2)
                .enable_all()
                .build()
                .unwrap();
            runtime.block_on(async {
                let mut term =
                    tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                        .unwrap();
                let mut floods = Vec::new();
                for _ in 0..2 {
                    floods.push(tokio::spawn(async {
                        let payload = "x".repeat(8192);
                        for _ in 0..2048 {
                            tracing::info!(target: "nyxmon.test", "{payload}");
                        }
                    }));
                }
                let heartbeat = tokio::spawn(async {
                    tokio::time::sleep(Duration::from_millis(10)).await;
                });
                let probe = tokio::spawn(async {
                    assert!(
                        tokio::time::timeout(
                            Duration::from_millis(20),
                            std::future::pending::<()>()
                        )
                        .await
                        .is_err()
                    );
                });
                for flood in floods {
                    flood.await.unwrap();
                }
                heartbeat.await.unwrap();
                probe.await.unwrap();
                assert!(dropped_log_records() > 0);
                println!("LIVE");
                term.recv().await.unwrap();
                let before = std::time::Instant::now();
                shutdown_logging();
                assert!(before.elapsed() < Duration::from_secs(1));
                println!("STOPPED");
            });
            return;
        }
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "logging::tests::unread_stderr_keeps_both_workers_timers_and_sigterm_live",
                "--nocapture",
            ])
            .env(CHILD, "1")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        // Keep stderr open but never read it. More than 32 MiB is submitted.
        let stdout = child.stdout.take().unwrap();
        let (lines, received) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                if lines.send(line.unwrap()).is_err() {
                    break;
                }
            }
        });
        let result = (|| -> Result<(), String> {
            for expected in ["LIVE", "STOPPED"] {
                loop {
                    let line = received
                        .recv_timeout(Duration::from_secs(5))
                        .map_err(|e| e.to_string())?;
                    if line == expected {
                        break;
                    }
                }
                if expected == "LIVE" {
                    assert!(
                        Command::new("kill")
                            .args(["-TERM", &child.id().to_string()])
                            .status()
                            .unwrap()
                            .success()
                    );
                }
            }
            Ok(())
        })();
        // Bound failure cleanup as well, even if both old workers wedged.
        if result.is_err() {
            let _ = child.kill();
        }
        let status = child.wait().unwrap();
        assert!(
            result.is_ok(),
            "runtime or signal handling stalled: {result:?}"
        );
        assert!(status.success());
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
        let subscriber = PyLogSubscriber::stderr(LevelFilter::WARN).unwrap();
        let _guard = tracing::subscriber::set_default(subscriber);
        assert!(!tracing::enabled!(target: "nyxmon.x", Level::INFO));
        assert!(tracing::enabled!(target: "nyxmon.x", Level::WARN));
    }
}
