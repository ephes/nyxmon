//! A one-shot event (`threading.Event` / `anyio.Event` without `clear`).

use std::sync::atomic::{AtomicBool, Ordering};

use tokio::sync::Notify;

/// Set once, observed by any number of waiters.
#[derive(Debug, Default)]
pub struct Flag {
    set: AtomicBool,
    notify: Notify,
}

impl Flag {
    pub fn new() -> Self {
        Flag::default()
    }

    pub fn set(&self) {
        self.set.store(true, Ordering::SeqCst);
        self.notify.notify_waiters();
    }

    pub fn is_set(&self) -> bool {
        self.set.load(Ordering::SeqCst)
    }

    /// Return once the flag is set.
    pub async fn wait(&self) {
        loop {
            let notified = self.notify.notified();
            tokio::pin!(notified);
            // Register before checking, so a `set` in between is not lost.
            notified.as_mut().enable();
            if self.is_set() {
                return;
            }
            notified.await;
        }
    }
}

/// Wait for `flag`, or forever when there is none.
pub async fn wait_for(flag: Option<&Flag>) {
    match flag {
        Some(flag) => flag.wait().await,
        None => std::future::pending().await,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    #[tokio::test]
    async fn waiters_wake_once_the_flag_is_set() {
        let flag = Arc::new(Flag::new());
        let waiter = tokio::spawn({
            let flag = flag.clone();
            async move { flag.wait().await }
        });
        tokio::task::yield_now().await;
        assert!(!waiter.is_finished());
        flag.set();
        waiter.await.unwrap();
        // Already set: returns at once.
        flag.wait().await;
        assert!(flag.is_set());
    }
}
