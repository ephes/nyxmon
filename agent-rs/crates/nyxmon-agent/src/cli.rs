//! Command line (`cli.py:start_agent`, PLAN.md 5.1).

use clap::Parser;
use nyxmon_core::pyjson::py_int;

use crate::logging::LOG_LEVELS;

/// `type=int` of argparse: Python `int()` (surrounding whitespace, sign,
/// underscores between digits). Values beyond 64 bits are rejected.
fn python_int(value: &str) -> Result<i64, String> {
    let parsed = py_int(value).map_err(|_| format!("invalid int value: '{value}'"))?;
    i64::try_from(parsed).map_err(|_| format!("int value out of range: '{value}'"))
}

/// `--version` text after the program name: the package version and the git
/// commit the binary was built from (`build.rs`), e.g. `0.1.0 (27cf52e…)`.
/// The release workflow and the deployment role parse the first word.
pub const VERSION: &str = concat!(
    env!("CARGO_PKG_VERSION"),
    " (",
    env!("NYXMON_AGENT_GIT_COMMIT"),
    ")"
);

/// Run the NyxMon monitoring agent
#[derive(Debug, Parser)]
#[command(
    name = "nyxmon-agent",
    version = VERSION,
    about = "Run the NyxMon monitoring agent"
)]
pub struct Args {
    /// Path to SQLite database file
    #[arg(long)]
    pub db: String,

    /// Check interval in seconds (default: 5)
    #[arg(long, default_value = "5", value_parser = python_int, allow_hyphen_values = true)]
    pub interval: i64,

    /// Results cleanup interval in seconds (default: 3600 - 1 hour)
    #[arg(long, default_value = "3600", value_parser = python_int, allow_hyphen_values = true)]
    pub cleanup_interval: i64,

    /// Results retention period in seconds (default: 86400 - 24 hours)
    #[arg(long, default_value = "86400", value_parser = python_int, allow_hyphen_values = true)]
    pub retention_period: i64,

    /// Number of old results deleted per batch; each cleanup run deletes
    /// batches until the expired backlog is gone (default: 1000)
    #[arg(long, default_value = "1000", value_parser = python_int, allow_hyphen_values = true)]
    pub batch_size: i64,

    /// Disable the results cleaner
    #[arg(long)]
    pub disable_cleaner: bool,

    /// Set the logging level (default: INFO)
    #[arg(long, default_value = "INFO", value_parser = LOG_LEVELS)]
    pub log_level: String,

    /// Enable Telegram notifications (requires TELEGRAM_BOT_TOKEN and
    /// TELEGRAM_CHAT_ID env vars)
    #[arg(long)]
    pub enable_telegram: bool,

    /// Only verify the database schema against the agent contract (read-only,
    /// no lock) and exit 0 or 1
    #[arg(long)]
    pub check_schema: bool,

    /// Shadow run on a database copy: logging notifier with machine-readable
    /// lines; TELEGRAM_* and OPSGATE_* are ignored
    #[arg(long)]
    pub shadow: bool,
}

/// `str(pathlib.Path(value))` (POSIX): repeated and trailing slashes and
/// `.` components dropped, exactly two leading slashes kept, `.` when empty.
pub fn py_path_str(value: &str) -> String {
    let leading = if value.starts_with("//") && !value.starts_with("///") {
        "//"
    } else if value.starts_with('/') {
        "/"
    } else {
        ""
    };
    let parts: Vec<&str> = value
        .split('/')
        .filter(|part| !part.is_empty() && *part != ".")
        .collect();
    let joined = format!("{leading}{}", parts.join("/"));
    if joined.is_empty() {
        ".".to_owned()
    } else {
        joined
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<Args, clap::Error> {
        Args::try_parse_from(std::iter::once("nyxmon-agent").chain(args.iter().copied()))
    }

    #[test]
    fn defaults_match_start_agent() {
        let args = parse(&["--db", "db.sqlite3"]).unwrap();
        assert_eq!(
            (
                args.interval,
                args.cleanup_interval,
                args.retention_period,
                args.batch_size
            ),
            (5, 3600, 86400, 1000)
        );
        assert_eq!(args.log_level, "INFO");
        assert!(!args.disable_cleaner && !args.enable_telegram && !args.shadow);
        assert!(!args.check_schema);
    }

    #[test]
    fn integers_follow_python_int() {
        let args = parse(&[
            "--db",
            "x",
            "--interval",
            " +1_0 ",
            "--batch-size",
            "-3",
            "--log-level",
            "WARNING",
        ])
        .unwrap();
        assert_eq!((args.interval, args.batch_size), (10, -3));
        for bad in ["1.5", "ten", "1__0", "99999999999999999999"] {
            let err = parse(&["--db", "x", "--interval", bad]).unwrap_err();
            assert_eq!(err.exit_code(), 2, "{bad}");
        }
    }

    #[test]
    fn usage_errors_exit_2() {
        assert_eq!(parse(&[]).unwrap_err().exit_code(), 2);
        let err = parse(&["--db", "x", "--log-level", "info"]).unwrap_err();
        assert_eq!(err.exit_code(), 2);
    }

    #[test]
    fn paths_render_like_pathlib() {
        for (raw, rendered) in [
            ("db.sqlite3", "db.sqlite3"),
            ("./db.sqlite3", "db.sqlite3"),
            ("a//b/./c/", "a/b/c"),
            ("/srv/../db", "/srv/../db"),
            ("//host/db", "//host/db"),
            ("///x", "/x"),
            ("", "."),
            (".", "."),
        ] {
            assert_eq!(py_path_str(raw), rendered, "{raw}");
        }
    }
}
