//! The agent lock (PLAN.md section 3.6, D-10).
//!
//! The Rust agent resolves the existing regular database path before taking
//! a stable sibling lock. Symlink/relative spellings therefore share ownership.
//! The separate lock avoids interfering with SQLite's own locking.

use rustix::fs::OFlags;
use std::ffi::OsString;
use std::fmt;
use std::fs::{File, OpenOptions, TryLockError};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};

/// Suffix appended to the database path.
pub const LOCK_SUFFIX: &str = ".agent.lock";

/// Append the lock suffix to a database path. Acquisition supplies the canonical path.
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
    database_path: PathBuf,
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
        // Missing files (including dangling aliases) are refused: initialize the
        // database through Django before starting an agent.
        let database_path = db_path.canonicalize().map_err(|source| LockError::Io {
            path: db_path.to_owned(),
            source,
        })?;
        let metadata = database_path.metadata().map_err(|source| LockError::Io {
            path: database_path.clone(),
            source,
        })?;
        if !metadata.is_file() || metadata.nlink() != 1 {
            return Err(LockError::Io {
                path: database_path,
                source: std::io::Error::other("database must be a regular file with one hard link"),
            });
        }
        let path = agent_lock_path(&database_path);
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
            .custom_flags((OFlags::NOFOLLOW | OFlags::NONBLOCK).bits() as i32)
            .open(&path)
            .map_err(io_error)?;
        let metadata = file.metadata().map_err(io_error)?;
        if !metadata.is_file() || metadata.nlink() != 1 {
            return Err(io_error(std::io::Error::other(
                "agent lock must be a regular file with one hard link",
            )));
        }
        // `File::try_lock` is `flock(LOCK_EX | LOCK_NB)` on Unix, the same
        // lock Python's `fcntl.flock` takes.
        match file.try_lock() {
            Ok(()) => Ok(AgentLock {
                path,
                database_path,
                _file: file,
            }),
            Err(TryLockError::WouldBlock) => Err(LockError::Held { path }),
            Err(TryLockError::Error(source)) => Err(io_error(source)),
        }
    }

    /// The admitted canonical database path, also used to open SQLite so the
    /// caller's alias cannot be retargeted between locking and opening.
    pub fn database_path(&self) -> &Path {
        &self.database_path
    }

    /// The lock file path.
    pub fn path(&self) -> &Path {
        &self.path
    }
}
