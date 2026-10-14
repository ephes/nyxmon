//! The `nyxmon-agent` binary.
//!
//! Slice S9 adds the CLI, logging, startup, runner, result handling and the
//! collector (PLAN.md sections 5.1, 5.3, 5.8-5.10). Until then the binary only
//! reports its version and refuses to run.

use std::process::ExitCode;

fn main() -> ExitCode {
    let version = env!("CARGO_PKG_VERSION");
    if std::env::args().nth(1).as_deref() == Some("--version") {
        println!("nyxmon-agent {version}");
        return ExitCode::SUCCESS;
    }
    eprintln!("nyxmon-agent {version}: the agent runtime is not implemented yet");
    ExitCode::FAILURE
}
