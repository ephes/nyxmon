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

    /// Poll the real actor call once and acknowledge enqueueing before returning.
    /// Every use holds the actor at a channel gate, so a reply cannot race this poll.
    async fn enqueue<T: Send + 'static>(
        store: &Store,
        operation: impl FnOnce(&mut Connection) -> Result<T, StoreError> + Send + 'static,
    ) -> tokio::task::JoinHandle<Result<T, StoreError>> {
        use std::task::Poll;
        let store = store.clone();
        let (sent, queued) = tokio::sync::oneshot::channel();
        let task = tokio::spawn(async move {
            let call = store.call(operation);
            tokio::pin!(call);
            assert!(
                std::future::poll_fn(|cx| Poll::Ready(call.as_mut().poll(cx)))
                    .await
                    .is_pending()
            );
            sent.send(()).unwrap();
            call.await
        });
        bounded(queued).await.unwrap();
        task
    }

    async fn bounded<T>(future: impl std::future::Future<Output = T>) -> T {
        tokio::time::timeout(Duration::from_secs(5), future)
            .await
            .expect("actor must settle without a timing sleep")
    }

    #[tokio::test]
    async fn cancelled_writes_commit_once_in_fifo_order() {
        // Cancel both a queued job and a transaction paused just before commit.
        for cancel_in_transaction in [false, true] {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("db.sqlite3");
            let raw = Connection::open(&path).unwrap();
            raw.execute_batch("CREATE TABLE writes (value INTEGER);")
                .unwrap();
            let store = Store::open(&path).unwrap();
            let (entered, started) = tokio::sync::oneshot::channel();
            let (release, gate) = mpsc::channel();
            let blocker = if cancel_in_transaction {
                None
            } else {
                let blocker = enqueue(&store, move |_| {
                    entered.send(()).unwrap();
                    gate.recv().unwrap();
                    Ok(())
                })
                .await;
                bounded(started).await.unwrap();
                Some((blocker, release))
            };
            let (entered_write, write_started) = tokio::sync::oneshot::channel();
            let (release_write, write_gate) = mpsc::channel();
            let write = enqueue(&store, move |connection| {
                let tx = connection
                    .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
                tx.execute("INSERT INTO writes VALUES (10)", [])?;
                entered_write.send(()).unwrap();
                write_gate.recv().unwrap();
                tx.commit()?;
                Ok(())
            })
            .await;
            if let Some((blocker, release)) = blocker {
                write.abort();
                assert!(bounded(write).await.unwrap_err().is_cancelled());
                // The caller is gone before the actor starts the write.
                release.send(()).unwrap();
                bounded(blocker).await.unwrap().unwrap();
                bounded(write_started).await.unwrap();
            } else {
                bounded(write_started).await.unwrap();
                write.abort();
                assert!(bounded(write).await.unwrap_err().is_cancelled());
            }
            // Queue a sibling while the cancelled write still owns its transaction.
            let later = enqueue(&store, |connection| {
                connection.execute("INSERT INTO writes SELECT value + 1 FROM writes", [])?;
                Ok(())
            })
            .await;
            release_write.send(()).unwrap();
            bounded(later).await.unwrap().unwrap();
            let values = store.call(|connection| {
                Ok(connection
                    .prepare("SELECT value FROM writes ORDER BY rowid")?
                    .query_map([], |r| r.get::<_, i64>(0))?
                    .collect::<rusqlite::Result<Vec<_>>>()?)
            });
            assert_eq!(bounded(values).await.unwrap(), [10, 11]);
            let count: i64 = raw
                .query_row("SELECT COUNT(*) FROM writes", [], |r| r.get(0))
                .unwrap();
            assert_eq!(count, 2);
        }
    }

    #[tokio::test]
    async fn dropping_all_handles_drains_queued_work_and_closes_the_connection() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("db.sqlite3");
        let raw = Connection::open(&path).unwrap();
        raw.busy_timeout(Duration::from_secs(5)).unwrap();
        raw.execute_batch("CREATE TABLE writes (value INTEGER)")
            .unwrap();
        let store = Store::open(&path).unwrap();
        let clone = store.clone();
        let (entered, started) = tokio::sync::oneshot::channel();
        let (release, gate) = mpsc::channel();
        let blocker = enqueue(&store, move |_| {
            entered.send(()).unwrap();
            gate.recv().unwrap();
            Ok(())
        })
        .await;
        bounded(started).await.unwrap();
        let (done, completed) = tokio::sync::oneshot::channel();
        // Use the existing private Job seam so no awaiting task holds a Store clone.
        store
            .jobs
            .send(Box::new(move |connection| {
                connection
                    .execute_batch(
                        "BEGIN IMMEDIATE; INSERT INTO writes VALUES (7); COMMIT; BEGIN IMMEDIATE;",
                    )
                    .unwrap();
                done.send(()).unwrap();
                // Leave a write lock behind: only closing the actor connection releases it.
            }))
            .unwrap();
        // Cancelling the gate caller drops its clone, but its queued operation remains.
        blocker.abort();
        assert!(bounded(blocker).await.unwrap_err().is_cancelled());
        drop(clone);
        drop(store);
        release.send(()).unwrap();
        bounded(completed).await.unwrap();
        raw.execute_batch("BEGIN IMMEDIATE")
            .expect("last handle must close actor connection");
        assert_eq!(
            raw.query_row("SELECT value FROM writes", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            7
        );
        raw.execute_batch("ROLLBACK").unwrap();
    }

    #[tokio::test]
    async fn panicking_transaction_rolls_back_and_rejects_queued_siblings() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("db.sqlite3");
        Connection::open(&path)
            .unwrap()
            .execute_batch("CREATE TABLE writes (value INTEGER)")
            .unwrap();
        let store = Store::open(&path).unwrap();
        let (entered, started) = tokio::sync::oneshot::channel();
        let (release, gate) = mpsc::channel();
        let panicking = enqueue(&store, move |connection| -> Result<(), StoreError> {
            // Deliberately leave rollback to Connection's drop on actor-thread unwind.
            connection.execute_batch("BEGIN IMMEDIATE; INSERT INTO writes VALUES (42)")?;
            entered.send(()).unwrap();
            gate.recv().unwrap();
            panic!("synthetic transaction failure");
        })
        .await;
        bounded(started).await.unwrap();
        let mut siblings = Vec::new();
        for _ in 0..3 {
            siblings.push(
                enqueue(&store, |connection| {
                    connection.execute("INSERT INTO writes VALUES (99)", [])?;
                    Ok(())
                })
                .await,
            );
        }
        release.send(()).unwrap();
        assert!(matches!(
            bounded(panicking).await.unwrap(),
            Err(StoreError::ActorDied)
        ));
        for sibling in siblings {
            assert!(matches!(
                bounded(sibling).await.unwrap(),
                Err(StoreError::ActorDied)
            ));
        }
        assert!(matches!(
            bounded(store.call(|_| Ok(()))).await,
            Err(StoreError::ActorDied)
        ));
        let raw = Connection::open(&path).unwrap();
        assert_eq!(
            raw.query_row("SELECT COUNT(*) FROM writes", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            0
        );
        let restarted = Store::open(&path).unwrap();
        bounded(restarted.call(|connection| {
            connection.execute("INSERT INTO writes VALUES (7)", [])?;
            Ok(())
        }))
        .await
        .unwrap();
        assert_eq!(
            raw.query_row("SELECT value FROM writes", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            7
        );
    }
}
