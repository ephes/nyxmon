//! The agent lock (PLAN.md section 3.6, D-10).
//!
//! Both agents take a non-blocking exclusive `flock` on `<db path>.agent.lock`
//! before touching the database and hold it until exit, so a second agent on
//! the same database refuses to start. The Python side is
//! `src/nyxmon/agent_lock.py`. The lock is a separate file: `flock` on the
//! database itself would interfere with SQLite's own locks.

use std::ffi::OsString;
use std::fmt;
use std::fs::{File, OpenOptions, TryLockError};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

/// Suffix appended to the database path.
pub const LOCK_SUFFIX: &str = ".agent.lock";

/// `<db path>.agent.lock`, derived from the path as given.
pub fn agent_lock_path(db_path: &Path) -> PathBuf {
    let mut path = OsString::from(db_path.as_os_str());
    path.push(LOCK_SUFFIX);
    PathBuf::from(path)
}

/// Why the lock was not taken.
#[derive(Debug)]
pub enum LockError {
    /// Another process holds the lock.
    Held { path: PathBuf },
    /// The lock file cannot be opened or locked.
    Io {
        path: PathBuf,
        source: std::io::Error,
    },
}

impl fmt::Display for LockError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            LockError::Held { path } => write!(
                f,
                "another nyxmon agent holds {}; refusing to start",
                path.display()
            ),
            LockError::Io { path, source } => {
                write!(f, "cannot take the agent lock {}: {source}", path.display())
            }
        }
    }
}

impl std::error::Error for LockError {}

/// A held agent lock; released when dropped (or when the process exits).
#[derive(Debug)]
pub struct AgentLock {
    path: PathBuf,
    _file: File,
}

impl AgentLock {
    /// Take the lock of the database at `db_path` without blocking. The
    /// lock file is created with mode 0600 if missing.
    ///
    /// # Errors
    /// [`LockError::Held`] when another process (or another open of the
    /// file) holds it, [`LockError::Io`] otherwise.
    pub fn acquire(db_path: &Path) -> Result<AgentLock, LockError> {
        let path = agent_lock_path(db_path);
        let io_error = |source| LockError::Io {
            path: path.clone(),
            source,
        };
        // std opens with O_CLOEXEC, so ping children do not inherit it.
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .open(&path)
            .map_err(io_error)?;
        // `File::try_lock` is `flock(LOCK_EX | LOCK_NB)` on Unix, the same
        // lock Python's `fcntl.flock` takes.
        match file.try_lock() {
            Ok(()) => Ok(AgentLock { path, _file: file }),
            Err(TryLockError::WouldBlock) => Err(LockError::Held { path }),
            Err(TryLockError::Error(source)) => Err(io_error(source)),
        }
    }

    /// The lock file path.
    pub fn path(&self) -> &Path {
        &self.path
    }
}
