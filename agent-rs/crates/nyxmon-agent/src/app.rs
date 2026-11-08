//! Startup and orchestration (`cli.py:start_agent`,
//! `run_monitoring_services`; PLAN.md 5.1, 5.8 shutdown).

use std::path::Path;
use std::process::ExitCode;
use std::sync::Arc;

use nyxmon_checks::registry::{Executors, Registry};
use nyxmon_core::env::{Env, SiteMode, notifier_env, site_env_config};
use nyxmon_core::log::{TracingSink, WarnOnce};
use nyxmon_notify::Notifier;
use nyxmon_notify::logging::LoggingNotifier;
use nyxmon_notify::telegram::{NotifierSettings, TelegramNotifier};
use nyxmon_store::{AgentLock, Store, check_schema};
use tokio::signal::unix::{SignalKind, signal};

use crate::cleaner::{Cleaner, DEFAULT_MAX_BATCHES_PER_CYCLE};
use crate::cli::{Args, py_path_str};
use crate::collector::Collector;
use crate::results::{ResultHandler, system_clock};
use crate::site::SiteRuntime;
use crate::startup::validate_check_types;
use crate::sync::Flag;

macro_rules! cli_error {
    ($($arg:tt)*) => { tracing::error!(target: "nyxmon.entrypoints.cli", $($arg)*) };
}
macro_rules! cli_info {
    ($($arg:tt)*) => { tracing::info!(target: "nyxmon.entrypoints.cli", $($arg)*) };
}

/// Log `Error running monitoring services` with the cause and exit 1.
fn fatal(cause: impl std::fmt::Display) -> ExitCode {
    cli_error!("Error running monitoring services\n{cause}");
    ExitCode::FAILURE
}

/// `--check-schema`: read-only verification, exit 0 or 1.
pub fn check_schema_only(db: &str) -> ExitCode {
    if !Path::new(db).exists() {
        cli_error!("Database file not found: {}", py_path_str(db));
        return ExitCode::FAILURE;
    }
    match check_schema(Path::new(db)) {
        Ok(report) if report.is_ok() => {
            cli_info!("{report}");
            ExitCode::SUCCESS
        }
        Ok(report) => {
            cli_error!("{report}");
            ExitCode::FAILURE
        }
        Err(err) => {
            cli_error!("cannot verify the database schema: {err}");
            ExitCode::FAILURE
        }
    }
}

/// `start_agent` after argument parsing and logging setup.
pub fn start_agent(args: &Args) -> ExitCode {
    if args.check_schema {
        return check_schema_only(&args.db);
    }
    if args.shadow && args.enable_telegram {
        cli_error!("--shadow cannot be combined with --enable-telegram; refusing to start");
        return ExitCode::FAILURE;
    }
    // Also guard programmatic callers before opening the DB or spawning tasks.
    if args.interval <= 0 || args.cleanup_interval <= 0 || args.retention_period <= 0 {
        return fatal("interval, cleanup_interval and retention_period must be strictly positive");
    }
    let db_path = Path::new(&args.db);
    if !db_path.exists() {
        cli_error!("Database file not found: {}", py_path_str(&args.db));
        return ExitCode::FAILURE;
    }
    // One agent per database, Python or Rust (PLAN.md 3.6), held until exit.
    let agent_lock = match AgentLock::acquire(db_path) {
        Ok(lock) => lock,
        Err(err) => {
            cli_error!("{err}");
            return ExitCode::FAILURE;
        }
    };
    cli_info!(
        "Initializing monitoring services with database: {}",
        args.db
    );
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .max_blocking_threads(32)
        .enable_all()
        .build()
    {
        Ok(runtime) => runtime,
        Err(err) => return fatal(err),
    };
    let code = runtime.block_on(run_monitoring_services(args, agent_lock.database_path()));
    // Executor threads blocked in the system resolver must not hold the
    // exit (Python's daemon threads).
    runtime.shutdown_background();
    code
}

/// `run_monitoring_services`.
async fn run_monitoring_services(args: &Args, db_path: &Path) -> ExitCode {
    let store = match Store::open(db_path) {
        Ok(store) => store,
        Err(err) => return fatal(err),
    };
    match store.verify_schema().await {
        Ok(report) if report.is_ok() => cli_info!("{report}"),
        Ok(report) => {
            cli_error!("{report}");
            return ExitCode::FAILURE;
        }
        Err(err) => return fatal(err),
    }

    let env = Arc::new(Env::process());
    let notifier: Arc<dyn Notifier> = if args.shadow {
        Arc::new(LoggingNotifier::new(true))
    } else if args.enable_telegram {
        let notifier_env = match notifier_env(&env) {
            Ok(notifier_env) => notifier_env,
            Err(err) => return fatal(format!("{}: {err}", err.python_class())),
        };
        let notifier = TelegramNotifier::new(&notifier_env, NotifierSettings::default());
        cli_info!("Telegram notifications enabled");
        Arc::new(notifier)
    } else {
        Arc::new(LoggingNotifier::new(false))
    };

    let cleaner = if args.disable_cleaner {
        None
    } else {
        match Cleaner::new(
            store.clone(),
            args.cleanup_interval,
            args.retention_period,
            args.batch_size,
            DEFAULT_MAX_BATCHES_PER_CYCLE,
        ) {
            Ok(cleaner) => Some(cleaner),
            Err(err) => return fatal(err),
        }
    };

    // `bootstrap`: the observer exists only when the mode is not off; its
    // warn-once state is the result handler's.
    let warnings = Arc::new(WarnOnce::new(Arc::new(TracingSink)));
    let site_config = site_env_config(&env);
    let site = if site_config.mode == SiteMode::Off {
        None
    } else {
        let mode = site_config.mode.as_str();
        let runtime = SiteRuntime::new(
            site_config,
            warnings.clone(),
            store.clone(),
            Some(notifier.clone()),
            system_clock(),
        );
        tracing::info!(target: "nyxmon.bootstrap", "site connectivity observation is {mode}");
        Some(runtime)
    };

    let executors: Arc<dyn Executors> = Arc::new(Registry::new());
    match store.list_checks().await {
        Ok(checks) => validate_check_types(&checks, executors.registered_types()),
        Err(err) => return fatal(err),
    }

    // Registered before the collector starts, so a signal can never take
    // the default action while claims are held.
    let (mut sigterm, mut sigint) = match (
        signal(SignalKind::terminate()),
        signal(SignalKind::interrupt()),
    ) {
        (Ok(sigterm), Ok(sigint)) => (sigterm, sigint),
        (Err(err), _) | (_, Err(err)) => return fatal(err),
    };

    let mut handler = ResultHandler::new(store.clone(), notifier, env.clone(), warnings);
    if let Some(site) = &site {
        handler = handler.with_site_state(site.state());
    }
    let handler = Arc::new(handler);
    let collector = Arc::new(Collector::new(
        store,
        handler,
        executors,
        env,
        args.interval,
    ));
    let collecting = tokio::spawn(collector.clone().run());
    // A second task beside the collector: probing never blocks collection.
    let site_stop = Arc::new(Flag::new());
    let observing = site.map(|site| tokio::spawn(site.run(site_stop.clone())));
    let cleaning = cleaner.map(|cleaner| tokio::spawn(async move { cleaner.run().await }));
    cli_info!("Monitoring started with {}s check interval", args.interval);

    let shutdown = async {
        tokio::select! {
            _ = sigterm.recv() => { received(15); },
            _ = sigint.recv() => { received(2); },
        }
    };
    let task_failed =
        crate::supervision::supervise(collecting, observing, cleaning, shutdown, || {
            collector.request_stop();
            site_stop.set();
        })
        .await;
    monitoring_exit(task_failed, collector.failed())
}

fn monitoring_exit(task_failed: bool, database_failed: bool) -> ExitCode {
    if database_failed {
        return fatal("the database thread is gone");
    }
    if task_failed {
        ExitCode::FAILURE
    } else {
        ExitCode::SUCCESS
    }
}

fn received(signum: i32) {
    cli_info!("Received signal {signum}, shutting down...");
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::Parser;

    #[test]
    fn database_failure_keeps_its_diagnostic_after_task_completion() {
        let (capture, _guard) = crate::logging::capture();
        assert_eq!(monitoring_exit(true, true), ExitCode::FAILURE);
        assert!(capture.contains("the database thread is gone"));
        assert_eq!(monitoring_exit(true, false), ExitCode::FAILURE);
        assert_eq!(monitoring_exit(false, false), ExitCode::SUCCESS);
    }

    #[test]
    fn invalid_programmatic_configuration_fails_before_opening_the_database() {
        for field in ["interval", "cleanup_interval", "retention_period"] {
            for value in [-1, 0] {
                let mut args = Args::try_parse_from(["nyxmon-agent", "--db", "unused"]).unwrap();
                match field {
                    "interval" => args.interval = value,
                    "cleanup_interval" => args.cleanup_interval = value,
                    _ => args.retention_period = value,
                }
                let (capture, _guard) = crate::logging::capture();
                assert_eq!(start_agent(&args), ExitCode::FAILURE);
                assert!(
                    capture
                        .messages()
                        .join("\n")
                        .contains("must be strictly positive")
                );
            }
        }
    }
}
