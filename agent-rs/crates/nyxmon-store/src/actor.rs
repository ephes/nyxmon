//! The DB actor: one OS thread owning the read-write connection
//! (PLAN.md section 3.2).

use std::path::Path;
use std::sync::mpsc;
use std::time::Duration;

use rusqlite::{Connection, OpenFlags};

use crate::error::StoreError;

type Job = Box<dyn FnOnce(&mut Connection) + Send>;

/// Connection settings.
#[derive(Clone, Copy, Debug)]
pub struct StoreOptions {
    /// How long a statement waits for another connection's lock before
    /// failing with `SQLITE_BUSY`. Python's `sqlite3.connect` default is
    /// `timeout=5.0`.
    pub busy_timeout: Duration,
}

impl Default for StoreOptions {
    fn default() -> Self {
        StoreOptions {
            busy_timeout: Duration::from_millis(5000),
        }
    }
}

/// Cloneable handle to the DB actor. Dropping the last clone ends the actor
/// thread and closes the connection.
///
/// An operation that was sent runs to completion on the actor thread even if
/// the awaiting future is dropped: a transaction is never cut short by
/// cancellation (the result is then discarded).
#[derive(Clone, Debug)]
pub struct Store {
    jobs: mpsc::Sender<Job>,
}

impl Store {
    /// Open the existing database read-write (never create it) with the
    /// default [`StoreOptions`] and start the actor thread.
    ///
    /// # Errors
    /// The SQLite open error, or [`StoreError::Io`] when the thread cannot be
    /// spawned.
    pub fn open(path: &Path) -> Result<Store, StoreError> {
        Store::open_with(path, StoreOptions::default())
    }

    /// [`Store::open`] with explicit options.
    ///
    /// # Errors
    /// See [`Store::open`].
    pub fn open_with(path: &Path, options: StoreOptions) -> Result<Store, StoreError> {
        let mut connection = open_read_write(path, options)?;
        let (jobs, inbox) = mpsc::channel::<Job>();
        std::thread::Builder::new()
            .name("nyxmon-db".to_owned())
            .spawn(move || {
                // A panicking job unwinds this thread: the connection is
                // dropped (rolling back its open transaction), every waiting
                // and later caller gets `ActorDied`.
                while let Ok(job) = inbox.recv() {
                    job(&mut connection);
                }
            })
            .map_err(StoreError::Io)?;
        Ok(Store { jobs })
    }

    /// Run `operation` on the actor thread and await its result.
    pub(crate) async fn call<T, F>(&self, operation: F) -> Result<T, StoreError>
    where
        T: Send + 'static,
        F: FnOnce(&mut Connection) -> Result<T, StoreError> + Send + 'static,
    {
        let (reply, response) = tokio::sync::oneshot::channel();
        let job: Job = Box::new(move |connection| {
            // The receiver is gone when the caller was cancelled; the
            // operation has completed either way.
            let _ = reply.send(operation(connection));
        });
        self.jobs.send(job).map_err(|_| StoreError::ActorDied)?;
        response.await.map_err(|_| StoreError::ActorDied)?
    }
}

/// The agent's connection: read-write, no create (the file must exist, like
/// `cli.py:start_agent`'s `db_path.exists()` check), Python's busy timeout,
/// foreign keys on (Python enables them for the result persistence; every
/// agent insert targets an existing check, PLAN.md section 3.2).
fn open_read_write(path: &Path, options: StoreOptions) -> Result<Connection, StoreError> {
    let flags = OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX;
    let connection = Connection::open_with_flags(path, flags)?;
    connection.busy_timeout(options.busy_timeout)?;
    connection.execute_batch("PRAGMA foreign_keys = ON")?;
    Ok(connection)
}

/// A read-only connection (`--check-schema`, `run-check`; PLAN.md 3.6).
pub(crate) fn open_read_only(path: &Path) -> Result<Connection, StoreError> {
    let flags = OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX;
    let connection = Connection::open_with_flags(path, flags)?;
    connection.busy_timeout(StoreOptions::default().busy_timeout)?;
    Ok(connection)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn a_panicking_operation_kills_the_actor() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("db.sqlite3");
        Connection::open(&path).unwrap();
        let store = Store::open(&path).unwrap();
        assert_eq!(store.call(|_| Ok(1)).await.unwrap(), 1);

        let died = store
            .call::<(), _>(|_| panic!("operation panicked"))
            .await
            .unwrap_err();
        assert!(matches!(died, StoreError::ActorDied));
        let later = store.call(|_| Ok(2)).await.unwrap_err();
        assert!(matches!(later, StoreError::ActorDied));
    }

    #[test]
    fn open_never_creates_the_database() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("missing.sqlite3");
        assert!(Store::open(&path).is_err());
        assert!(open_read_only(&path).is_err());
        assert!(!path.exists());
    }
}
