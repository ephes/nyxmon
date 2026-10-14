//! Two agents on one database: the second one refuses to start, whichever
//! implementation holds the lock (PLAN.md 3.6, acceptance of slice S2).
//!
//! Every lock holder and contender runs in its own process: a Rust child is
//! this test binary re-executed in "child" mode, a Python child runs
//! `src/nyxmon/agent_lock.py` (the module `cli.py:start_agent` uses) with a
//! bare `python3`.

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};

use nyxmon_store::{AgentLock, LockError, agent_lock_path};

const CHILD_ENV: &str = "NYXMON_STORE_TEST_LOCK_DB";
const HELD_MESSAGE_PREFIX: &str = "another nyxmon agent holds ";

/// Child mode: take the lock like the agent does; exit 1 on contention,
/// otherwise report `LOCKED` and hold it until stdin closes.
#[test]
fn child_takes_the_agent_lock() {
    let Some(db) = std::env::var_os(CHILD_ENV) else {
        return;
    };
    match AgentLock::acquire(Path::new(&db)) {
        Ok(_lock) => {
            println!("LOCKED");
            std::io::stdout().flush().unwrap();
            let mut line = String::new();
            let _ = std::io::stdin().read_line(&mut line);
            std::process::exit(0);
        }
        Err(err) => {
            eprintln!("{err}");
            std::process::exit(1);
        }
    }
}

fn rust_child(db: &Path) -> Command {
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
        .args([
            "child_takes_the_agent_lock",
            "--exact",
            "--nocapture",
            "--test-threads=1",
        ])
        .env(CHILD_ENV, db);
    command
}

const PYTHON_CHILD: &str = r#"
import sys
from nyxmon.agent_lock import AgentLockHeld, acquire_agent_lock

try:
    acquire_agent_lock(sys.argv[1])
except AgentLockHeld as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
print("LOCKED", flush=True)
sys.stdin.readline()
"#;

fn python_child(db: &Path) -> Command {
    let src = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../src");
    let mut command = Command::new("python3");
    command
        .args(["-c", PYTHON_CHILD])
        .arg(db)
        .env("PYTHONPATH", src)
        .env("PYTHONDONTWRITEBYTECODE", "1");
    command
}

/// Start a holder and wait until it reports the lock.
fn hold(mut command: Command) -> Child {
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .expect("spawn the lock holder");
    let stdout = child.stdout.take().unwrap();
    let locked = BufReader::new(stdout)
        .lines()
        .map_while(Result::ok)
        .any(|line| line.ends_with("LOCKED"));
    assert!(locked, "the holder did not take the lock");
    child
}

fn release(mut holder: Child) {
    drop(holder.stdin.take());
    assert!(holder.wait().unwrap().success());
}

/// Run a contender to completion: `(status, stderr)`.
fn contend(mut command: Command) -> (ExitStatus, String) {
    let output = command
        .stdin(Stdio::null())
        .output()
        .expect("spawn the contender");
    (
        output.status,
        String::from_utf8_lossy(&output.stderr).into_owned(),
    )
}

fn database() -> (tempfile::TempDir, PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("db.sqlite3");
    std::fs::write(&db, b"").unwrap();
    (dir, db)
}

fn expected_message(db: &Path) -> String {
    format!(
        "{HELD_MESSAGE_PREFIX}{}; refusing to start",
        agent_lock_path(db).display()
    )
}

#[test]
fn second_rust_agent_exits_1() {
    let (_dir, db) = database();
    let holder = hold(rust_child(&db));

    let (status, stderr) = contend(rust_child(&db));

    assert_eq!(status.code(), Some(1));
    assert!(stderr.contains(&expected_message(&db)), "{stderr}");
    release(holder);
    // Released with the holder's process.
    let (status, _) = contend(rust_child(&db));
    assert_eq!(status.code(), Some(0));
}

#[test]
fn rust_agent_refuses_a_database_locked_by_python() {
    let (_dir, db) = database();
    let holder = hold(python_child(&db));

    let (status, stderr) = contend(rust_child(&db));
    assert_eq!(status.code(), Some(1));
    assert!(stderr.contains(&expected_message(&db)), "{stderr}");
    match AgentLock::acquire(&db) {
        Err(LockError::Held { path }) => assert_eq!(path, agent_lock_path(&db)),
        other => panic!("expected a held lock, got {other:?}"),
    }

    release(holder);
    let lock = AgentLock::acquire(&db).unwrap();
    assert_eq!(lock.path(), agent_lock_path(&db));
}

#[test]
fn python_agent_refuses_a_database_locked_by_rust() {
    let (_dir, db) = database();
    let holder = hold(rust_child(&db));

    let (status, stderr) = contend(python_child(&db));

    assert_eq!(status.code(), Some(1));
    assert!(stderr.contains(&expected_message(&db)), "{stderr}");
    release(holder);
}

#[test]
fn lock_file_is_private_and_next_to_the_database() {
    use std::os::unix::fs::PermissionsExt;

    let (_dir, db) = database();
    let lock = AgentLock::acquire(&db).unwrap();
    assert_eq!(
        lock.path().file_name().unwrap(),
        "db.sqlite3.agent.lock",
        "the lock is a separate file next to the database"
    );
    let mode = std::fs::metadata(lock.path()).unwrap().permissions().mode();
    assert_eq!(mode & 0o077, 0);
    // A second open in the same process is refused too (flock is per open).
    assert!(matches!(
        AgentLock::acquire(&db),
        Err(LockError::Held { .. })
    ));
}
