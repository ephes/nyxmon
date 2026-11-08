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

    fn poll_once<F: std::future::Future>(
        future: std::pin::Pin<&mut F>,
    ) -> std::task::Poll<F::Output> {
        future.poll(&mut std::task::Context::from_waker(std::task::Waker::noop()))
    }

    #[test]
    fn preset_and_repeated_set_return_immediately_for_late_waiters() {
        let flag = Flag::new();
        flag.set();
        flag.set();
        for _ in 0..32 {
            let mut waiter = Box::pin(flag.wait());
            assert!(poll_once(waiter.as_mut()).is_ready());
        }
        assert!(flag.is_set());
    }

    #[test]
    fn fanout_survives_cancellation_before_and_after_notification() {
        for cancel_after_set in [false, true] {
            let flag = Flag::new();
            let mut waiters: Vec<_> = (0..32).map(|_| Box::pin(flag.wait())).collect();
            for waiter in &mut waiters {
                assert!(poll_once(waiter.as_mut()).is_pending());
            }
            if cancel_after_set {
                flag.set();
            }
            drop(waiters.remove(0));
            if !cancel_after_set {
                flag.set();
            }
            // Polling a pending future resumes its real Notify await; checking
            // readiness directly fails instead of hanging if a wakeup was lost.
            for waiter in &mut waiters {
                assert!(poll_once(waiter.as_mut()).is_ready());
            }
            flag.set();
            assert!(poll_once(Box::pin(flag.wait()).as_mut()).is_ready());
        }
    }

    #[test]
    fn set_racing_registration_and_check_keeps_all_waiters_ready() {
        // Half the waiters are registered already; the rest race their first
        // poll with set on another thread. No scheduler yields or sleeps.
        for _ in 0..128 {
            let flag = Flag::new();
            let barrier = std::sync::Barrier::new(2);
            let mut early: Vec<_> = (0..8).map(|_| Box::pin(flag.wait())).collect();
            for waiter in &mut early {
                assert!(poll_once(waiter.as_mut()).is_pending());
            }
            drop(early.remove(0));
            let mut racing: Vec<_> = (0..8).map(|_| Box::pin(flag.wait())).collect();
            let mut pending = Vec::new();
            std::thread::scope(|scope| {
                let setter = scope.spawn(|| {
                    barrier.wait();
                    flag.set();
                });
                barrier.wait();
                for mut waiter in racing.drain(..) {
                    if poll_once(waiter.as_mut()).is_pending() {
                        pending.push(waiter);
                    }
                }
                setter.join().unwrap();
            });
            for waiter in early.iter_mut().chain(pending.iter_mut()) {
                assert!(poll_once(waiter.as_mut()).is_ready());
            }
            assert!(poll_once(Box::pin(flag.wait()).as_mut()).is_ready());
        }
    }
}
