//! The DB actor: one OS thread owning the read-write connection
//! (PLAN.md section 3.2).

use std::path::Path;
use std::sync::{Arc, Mutex, mpsc};
use std::time::Duration;

use rusqlite::{Connection, OpenFlags};
use tokio::sync::Semaphore;

use crate::error::StoreError;

/// Maximum queued plus running operations per store actor.
pub const MAX_PENDING_JOBS: usize = 32;

enum Command {
    Run(Job),
    Close,
}

type Job = Box<dyn FnOnce(&mut Connection) + Send>;
type CloseResult = Result<(), Arc<rusqlite::Error>>;

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
    jobs: mpsc::Sender<Command>,
    closing: Arc<Mutex<bool>>,
    finished: tokio::sync::watch::Receiver<Option<CloseResult>>,
    admission: Arc<Semaphore>,
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
        Self::open_inner(path, options, None)
    }

    /// Open an actor that retains singleton ownership until its connection closes,
    /// even if its caller times out or drops all async runtime tasks.
    /// The admitted database generation must still match around the SQLite open.
    pub fn open_locked(lock: crate::AgentLock) -> Result<Store, StoreError> {
        let path = lock.database_path().to_owned();
        Self::open_inner(&path, StoreOptions::default(), Some(lock))
    }

    fn open_inner(
        path: &Path,
        options: StoreOptions,
        lock: Option<crate::AgentLock>,
    ) -> Result<Store, StoreError> {
        let connection = open_read_write(path, options, lock.as_ref())?;
        Self::start_actor(connection, lock, retire_connection)
    }

    fn start_actor(
        connection: Connection,
        lock: Option<crate::AgentLock>,
        retire: impl FnOnce(Connection) -> rusqlite::Result<()> + Send + 'static,
    ) -> Result<Store, StoreError> {
        let (jobs, inbox) = mpsc::channel::<Command>();
        let (done, finished) = tokio::sync::watch::channel(None);
        std::thread::Builder::new()
            .name("nyxmon-db".to_owned())
            .spawn(move || {
                // Reverse local drop order closes SQLite before releasing ownership,
                // including during unwinding from a panicking operation.
                let agent_lock = lock;
                let mut connection = connection;
                // A panicking job unwinds this thread: the connection is
                // dropped (rolling back its open transaction), every waiting
                // and later caller gets `ActorDied`.
                while let Ok(command) = inbox.recv() {
                    match command {
                        Command::Run(job) => job(&mut connection),
                        Command::Close => break,
                    }
                }
                let result = retire(connection).map_err(Arc::new);
                drop(agent_lock);
                let _ = done.send(Some(result));
            })
            .map_err(StoreError::Io)?;
        Ok(Store {
            jobs,
            closing: Arc::new(Mutex::new(false)),
            finished,
            admission: Arc::new(Semaphore::new(MAX_PENDING_JOBS)),
        })
    }

    /// Close the actor after draining accepted work.
    /// Every caller receives the same SQLite close outcome after lock release.
    pub async fn close(&self) -> Result<(), StoreError> {
        {
            // The same short mutex serializes submission and the terminal marker.
            let mut closing = self.closing.lock().unwrap_or_else(|e| e.into_inner());
            if !*closing {
                *closing = true;
                self.admission.close();
                self.jobs
                    .send(Command::Close)
                    .map_err(|_| StoreError::ActorDied)?;
            }
        }
        let mut finished = self.finished.clone();
        loop {
            if let Some(result) = finished.borrow_and_update().as_ref() {
                return result.clone().map_err(StoreError::ConnectionClose);
            }
            finished
                .changed()
                .await
                .map_err(|_| StoreError::ActorDied)?;
        }
    }

    /// Run `operation` on the actor thread and await its result.
    pub(crate) async fn call<T, F>(&self, operation: F) -> Result<T, StoreError>
    where
        T: Send + 'static,
        F: FnOnce(&mut Connection) -> Result<T, StoreError> + Send + 'static,
    {
        let permit = self
            .admission
            .clone()
            .acquire_owned()
            .await
            .map_err(|_| StoreError::ActorDied)?;
        let (reply, response) = tokio::sync::oneshot::channel();
        let job: Job = Box::new(move |connection| {
            let _permit = permit;
            // The receiver is gone when the caller was cancelled; the
            // operation has completed either way.
            let _ = reply.send(operation(connection));
        });
        {
            let closing = self.closing.lock().unwrap_or_else(|e| e.into_inner());
            if *closing {
                return Err(StoreError::ActorDied);
            }
            self.jobs
                .send(Command::Run(job))
                .map_err(|_| StoreError::ActorDied)?;
        }
        response.await.map_err(|_| StoreError::ActorDied)?
    }
}

fn retire_connection(connection: Connection) -> rusqlite::Result<()> {
    connection.close().map_err(|(connection, error)| {
        // Give rusqlite's destructor its chance to close before releasing ownership.
        drop(connection);
        error
    })
}

/// The agent's connection: read-write, no create (the file must exist, like
/// `cli.py:start_agent`'s `db_path.exists()` check), Python's busy timeout,
/// foreign keys on (Python enables them for the result persistence; every
/// agent insert targets an existing check, PLAN.md section 3.2).
fn open_read_write(
    path: &Path,
    options: StoreOptions,
    lock: Option<&crate::AgentLock>,
) -> Result<Connection, StoreError> {
    let mut flags = OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX;
    if let Some(lock) = lock {
        lock.verify_database_generation().map_err(StoreError::Io)?;
        flags |= OpenFlags::SQLITE_OPEN_NOFOLLOW;
    }
    let connection = Connection::open_with_flags(path, flags)?;
    if let Some(lock) = lock {
        lock.verify_database_generation().map_err(StoreError::Io)?;
    }
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

    #[test]
    fn locked_open_refuses_a_replaced_database_without_mutating_either_generation() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("db.sqlite3");
        let original = dir.path().join("original.sqlite3");
        let alias = dir.path().join("alias.sqlite3");
        Connection::open(&path)
            .unwrap()
            .execute_batch("CREATE TABLE original (value); INSERT INTO original VALUES (1)")
            .unwrap();
        std::os::unix::fs::symlink(&path, &alias).unwrap();
        let lock = crate::AgentLock::acquire(&alias).unwrap();
        std::fs::rename(&path, &original).unwrap();
        Connection::open(&path)
            .unwrap()
            .execute_batch("CREATE TABLE replacement (value); INSERT INTO replacement VALUES (2)")
            .unwrap();

        let original_bytes = std::fs::read(&original).unwrap();
        let replacement_bytes = std::fs::read(&path).unwrap();
        let error = Store::open_locked(lock).unwrap_err();
        assert!(
            error.to_string().contains("database generation changed"),
            "{error}"
        );
        assert_eq!(std::fs::read(&original).unwrap(), original_bytes);
        assert_eq!(std::fs::read(&path).unwrap(), replacement_bytes);
        let _next = crate::AgentLock::acquire(&alias).unwrap();
    }

    #[tokio::test]
    async fn close_waits_for_admitted_write_and_is_terminal() {
        use crate::AgentLock;
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("db");
        Connection::open(&path)
            .unwrap()
            .execute_batch("CREATE TABLE writes (value INTEGER)")
            .unwrap();
        let lock = AgentLock::acquire(&path).unwrap();
        let store = Store::open(&path).unwrap();
        let (entered, started) = tokio::sync::oneshot::channel();
        let (release, gate) = mpsc::channel();
        let write = enqueue(&store, move |connection| {
            entered.send(()).unwrap();
            gate.recv().unwrap();
            connection.execute("INSERT INTO writes VALUES (7)", [])?;
            Ok(())
        })
        .await;
        bounded(started).await.unwrap();
        let closing = store.clone();
        let mut close = Box::pin(async move {
            let result = closing.close().await;
            drop(lock);
            result
        });
        let pending = std::future::poll_fn(|cx| std::task::Poll::Ready(close.as_mut().poll(cx)))
            .await
            .is_pending();
        let held = AgentLock::acquire(&path).is_err();
        release.send(()).unwrap();
        bounded(write).await.unwrap().unwrap();
        assert!(pending, "close returned before admitted write finished");
        assert!(held, "ownership transferred before drain");
        bounded(close).await.unwrap();
        bounded(store.close()).await.unwrap();
        assert!(store.call(|_| Ok(())).await.is_err());
        let _next = AgentLock::acquire(&path).unwrap();
        assert_eq!(
            Connection::open(&path)
                .unwrap()
                .query_row("SELECT value FROM writes", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            7
        );
    }

    #[tokio::test]
    async fn cancelled_close_retains_lock_until_actor_connection_closes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("db");
        Connection::open(&path).unwrap();
        let store = Store::open_locked(crate::AgentLock::acquire(&path).unwrap()).unwrap();
        let (entered, started) = tokio::sync::oneshot::channel();
        let (release, gate) = mpsc::channel();
        let write = enqueue(&store, move |_| {
            entered.send(()).unwrap();
            gate.recv().unwrap();
            Ok(())
        })
        .await;
        bounded(started).await.unwrap();
        {
            let close = store.close();
            tokio::pin!(close);
            assert!(
                std::future::poll_fn(|cx| std::task::Poll::Ready(close.as_mut().poll(cx)))
                    .await
                    .is_pending()
            );
        }
        // Simulate drain timeout/cancellation while a detached operation survives.
        assert!(store.call(|_| Ok(())).await.is_err());
        assert!(crate::AgentLock::acquire(&path).is_err());
        release.send(()).unwrap();
        bounded(write).await.unwrap().unwrap();
        bounded(store.close()).await.unwrap();
        let _next = crate::AgentLock::acquire(&path).unwrap();
    }

    #[tokio::test]
    async fn close_reports_retirement_failure_after_draining_and_releasing_ownership() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("db.sqlite3");
        let raw = Connection::open(&path).unwrap();
        raw.execute_batch("CREATE TABLE writes (value INTEGER)")
            .unwrap();
        let lock = crate::AgentLock::acquire(&path).unwrap();
        let (retiring, retirement_started) = tokio::sync::oneshot::channel();
        let (finish_retirement, retirement_gate) = mpsc::channel();
        let connection =
            open_read_write(lock.database_path(), StoreOptions::default(), Some(&lock)).unwrap();
        let store = Store::start_actor(connection, Some(lock), move |connection| {
            let value =
                connection.query_row("SELECT value FROM writes", [], |row| row.get::<_, i64>(0))?;
            connection.execute_batch("BEGIN IMMEDIATE")?;
            retiring.send(value).unwrap();
            retirement_gate.recv().unwrap();
            drop(connection);
            Err(rusqlite::Error::SqliteFailure(
                rusqlite::ffi::Error::new(rusqlite::ffi::SQLITE_BUSY),
                Some("synthetic close failure".to_owned()),
            ))
        })
        .unwrap();
        let (entered, started) = tokio::sync::oneshot::channel();
        let (release_write, write_gate) = mpsc::channel();
        let write = enqueue(&store, move |connection| {
            entered.send(()).unwrap();
            write_gate.recv().unwrap();
            connection.execute("INSERT INTO writes VALUES (7)", [])?;
            Ok(())
        })
        .await;
        bounded(started).await.unwrap();

        let peer = store.clone();
        let close = store.close();
        tokio::pin!(close);
        assert!(
            std::future::poll_fn(|cx| std::task::Poll::Ready(close.as_mut().poll(cx)))
                .await
                .is_pending()
        );
        release_write.send(()).unwrap();
        assert_eq!(bounded(retirement_started).await.unwrap(), 7);
        assert!(
            std::future::poll_fn(|cx| std::task::Poll::Ready(close.as_mut().poll(cx)))
                .await
                .is_pending()
        );
        assert!(matches!(
            crate::AgentLock::acquire(&path),
            Err(crate::LockError::Held { .. })
        ));
        finish_retirement.send(()).unwrap();

        bounded(write).await.unwrap().unwrap();
        for result in [bounded(close).await, bounded(peer.close()).await] {
            let Err(StoreError::ConnectionClose(error)) = result else {
                panic!("expected SQLite close error, got {result:?}");
            };
            assert!(
                error.to_string().contains("synthetic close failure"),
                "{error}"
            );
        }
        let _next = crate::AgentLock::acquire(&path).unwrap();
        raw.execute_batch("BEGIN IMMEDIATE; ROLLBACK").unwrap();
    }

    #[tokio::test]
    async fn pending_jobs_are_bounded_and_cancellation_keeps_admitted_writes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("db.sqlite3");
        let raw = Connection::open(&path).unwrap();
        raw.execute_batch(include_str!("../../../fixtures/schema/schema.sql"))
            .unwrap();
        raw.execute("INSERT INTO service (id, name) VALUES (1, 'synthetic')", [])
            .unwrap();
        for id in 1..=64 {
            raw.execute("INSERT INTO health_check (id, service_id, name, check_type, url, check_interval, next_check_time, processing_started_at, status, disabled, data) VALUES (?, 1, 'synthetic', 'http', 'https://example.test', 300, 1000, 0, 'idle', 0, '{}')", [id]).unwrap();
        }
        let store = Store::open(&path).unwrap();
        let (entered, started) = tokio::sync::oneshot::channel();
        let (release, gate) = mpsc::channel();
        let blocker = enqueue(&store, move |_| {
            entered.send(()).unwrap();
            gate.recv().unwrap();
            Ok(())
        })
        .await;
        bounded(started).await.unwrap();
        let mut calls = Vec::new();
        for id in 1..=64 {
            let handle = store.clone();
            let mut call = Box::pin(async move { handle.reschedule_checks(&[id], 100).await });
            assert!(
                std::future::poll_fn(|cx| std::task::Poll::Ready(call.as_mut().poll(cx)))
                    .await
                    .is_pending()
            );
            calls.push(call);
        }
        drop(calls); // unadmitted calls disappear; admitted writes must finish
        release.send(()).unwrap();
        bounded(blocker).await.unwrap().unwrap();
        // This FIFO read follows all the writes that were actually accepted.
        let checks = bounded(store.list_checks()).await.unwrap();
        let changed: Vec<_> = checks
            .iter()
            .filter(|c| c.next_check_time == 100)
            .map(|c| c.check_id)
            .collect();
        assert_eq!(changed, (1..=31).collect::<Vec<_>>());
        assert_eq!(
            bounded(store.reschedule_checks(&[64], 100)).await.unwrap(),
            1
        );
    }

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
        assert!(matches!(store.close().await, Err(StoreError::ActorDied)));
        assert!(matches!(store.close().await, Err(StoreError::ActorDied)));
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
            .send(Command::Run(Box::new(move |connection| {
                connection
                    .execute_batch(
                        "BEGIN IMMEDIATE; INSERT INTO writes VALUES (7); COMMIT; BEGIN IMMEDIATE;",
                    )
                    .unwrap();
                done.send(()).unwrap();
                // Leave a write lock behind: only closing the actor connection releases it.
            })))
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
