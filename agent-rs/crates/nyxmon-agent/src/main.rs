//! The `nyxmon-agent` binary: a drop-in for `start-agent` (PLAN.md 5.1),
//! plus `nyxmon-agent run-check` (PLAN.md 7.4).

use std::process::ExitCode;

use clap::Parser;
use nyxmon_agent::app::start_agent;
use nyxmon_agent::cli::Args;
use nyxmon_agent::logging::{PyLogSubscriber, level_filter};
use nyxmon_agent::run_check::{RunCheckArgs, run_check};

fn install_logging(log_level: &str) -> bool {
    let level = level_filter(log_level).expect("restricted by clap");
    if tracing::subscriber::set_global_default(PyLogSubscriber::stderr(level)).is_err() {
        eprintln!("cannot install the log subscriber");
        return false;
    }
    true
}

fn main() -> ExitCode {
    // `start-agent` takes no positional argument, so a leading `run-check`
    // cannot be a valid agent command line.
    if std::env::args_os()
        .nth(1)
        .is_some_and(|arg| arg == "run-check")
    {
        let args = RunCheckArgs::parse_from(
            std::iter::once("nyxmon-agent run-check".into()).chain(std::env::args_os().skip(2)),
        );
        if !install_logging(&args.log_level) {
            return ExitCode::FAILURE;
        }
        return run_check(&args);
    }
    let args = Args::parse();
    if !install_logging(&args.log_level) {
        return ExitCode::FAILURE;
    }
    start_agent(&args)
}
