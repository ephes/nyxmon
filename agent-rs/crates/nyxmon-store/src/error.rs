//! Store errors.

use std::fmt;

/// Why a store operation failed.
#[derive(Debug)]
pub enum StoreError {
    /// An SQLite error (busy timeout, constraint, I/O, ...). The operation's
    /// transaction was rolled back.
    Sqlite(rusqlite::Error),
    /// `NotificationStateConflict`: the notification state changed after the
    /// caller calculated its transition. The result, the completion and the
    /// state write were rolled back together.
    NotificationStateConflict { check_id: i64 },
    /// A JSON document could not be serialised (`json.dumps` raised).
    Json(String),
    /// The DB actor thread is gone (a panicking operation or a failed
    /// spawn). Fatal: the agent logs and exits 1 (PLAN.md section 3.2).
    ActorDied,
    /// Opening the database or spawning the actor thread failed.
    Io(std::io::Error),
}

impl fmt::Display for StoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            StoreError::Sqlite(err) => write!(f, "{err}"),
            StoreError::NotificationStateConflict { check_id } => {
                write!(f, "{check_id}")
            }
            StoreError::Json(message) => write!(f, "{message}"),
            StoreError::ActorDied => f.write_str("the database thread is gone"),
            StoreError::Io(err) => write!(f, "{err}"),
        }
    }
}

impl std::error::Error for StoreError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            StoreError::Sqlite(err) => Some(err),
            StoreError::Io(err) => Some(err),
            _ => None,
        }
    }
}

impl From<rusqlite::Error> for StoreError {
    fn from(err: rusqlite::Error) -> Self {
        StoreError::Sqlite(err)
    }
}

impl StoreError {
    /// Whether SQLite gave up waiting for a lock (`SQLITE_BUSY` after the
    /// busy timeout, Python's `database is locked`).
    pub fn is_busy(&self) -> bool {
        matches!(
            self,
            StoreError::Sqlite(rusqlite::Error::SqliteFailure(err, _))
                if err.code == rusqlite::ErrorCode::DatabaseBusy
        )
    }
}
