//! The `nyxmon-agent` binary: a drop-in for `start-agent` (PLAN.md 5.1).

use std::process::ExitCode;

use clap::Parser;
use nyxmon_agent::app::start_agent;
use nyxmon_agent::cli::Args;
use nyxmon_agent::logging::{PyLogSubscriber, level_filter};

fn main() -> ExitCode {
    let args = Args::parse();
    let level = level_filter(&args.log_level).expect("restricted by clap");
    if tracing::subscriber::set_global_default(PyLogSubscriber::stderr(level)).is_err() {
        eprintln!("cannot install the log subscriber");
        return ExitCode::FAILURE;
    }
    start_agent(&args)
}
