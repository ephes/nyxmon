//! Synthetic connection attempts and blocked lookups.
use super::*;
use std::sync::mpsc;

#[tokio::test]
async fn cancelling_lookups_keeps_capacity_until_the_closures_exit() {
    let slots = Arc::new(Semaphore::new(2));
    let mut releases = Vec::new();
    for _ in 0..2 {
        let (release, blocked) = mpsc::channel();
        let (started, entered) = tokio::sync::oneshot::channel();
        let pool = Arc::clone(&slots);
        let caller = tokio::spawn(system_lookup_with_slots(pool, move || {
            started.send(()).unwrap();
            blocked.recv().unwrap();
        }));
        entered.await.unwrap();
        caller.abort();
        assert!(caller.await.unwrap_err().is_cancelled());
        releases.push(release);
    }
    assert_eq!(slots.available_permits(), 0);
    // Cancellation must not let later batches create more blocking jobs.
    // Their admission wait is bounded by the caller's elapsed budget.
    tokio::time::pause();
    for _ in 0..3 {
        let (result, ()) = tokio::join!(
            tokio::time::timeout(
                Duration::from_secs(1),
                system_lookup_with_slots(Arc::clone(&slots), || panic!(
                    "exhausted pool admitted work"
                )),
            ),
            async {
                // Active blocking work prevents automatic clock advance.
                tokio::task::yield_now().await;
                tokio::time::advance(Duration::from_secs(2)).await;
            }
        );
        assert!(result.is_err());
        assert_eq!(slots.available_permits(), 0);
    }
    tokio::time::resume();
    for release in releases {
        release.send(()).unwrap();
    }
    assert_eq!(
        system_lookup_with_slots(Arc::clone(&slots), || 42)
            .await
            .unwrap(),
        42
    );
    // Both old closures have returned when both permits can be acquired.
    let all = slots.acquire_many_owned(2).await.unwrap();
    drop(all);
}

#[test]
fn a_queued_lookup_also_retains_its_permit_after_cancellation() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .max_blocking_threads(1)
        .build()
        .unwrap();
    runtime.block_on(async {
        let slots = Arc::new(Semaphore::new(2));
        let (release_first, blocked_first) = mpsc::channel();
        let (first_started, first_entered) = tokio::sync::oneshot::channel();
        let first = tokio::spawn(system_lookup_with_slots(Arc::clone(&slots), move || {
            first_started.send(()).unwrap();
            blocked_first.recv().unwrap();
        }));
        first_entered.await.unwrap();
        let (release_second, blocked_second) = mpsc::channel();
        let (second_started, second_entered) = tokio::sync::oneshot::channel();
        let second = tokio::spawn(system_lookup_with_slots(Arc::clone(&slots), move || {
            second_started.send(()).unwrap();
            blocked_second.recv().unwrap();
        }));
        // A yield lets the second caller enqueue its job behind the first.
        tokio::task::yield_now().await;
        assert_eq!(slots.available_permits(), 0);
        first.abort();
        second.abort();
        assert!(first.await.unwrap_err().is_cancelled());
        assert!(second.await.unwrap_err().is_cancelled());
        assert_eq!(slots.available_permits(), 0);
        release_first.send(()).unwrap();
        second_entered.await.unwrap();
        assert_eq!(slots.available_permits(), 1);
        release_second.send(()).unwrap();
        let all = slots.acquire_many_owned(2).await.unwrap();
        drop(all);
    });
}

#[tokio::test]
async fn mail_connect_tries_each_distinct_address_in_resolver_order() {
    let first = "[fe80::1%1]:993".parse::<SocketAddr>().unwrap();
    let second = "[fe80::1%2]:993".parse::<SocketAddr>().unwrap();
    let reachable = "192.0.2.1:993".parse::<SocketAddr>().unwrap();
    let mut attempts = Vec::new();

    let connected = connect_addresses(
        [first, first, second, first, second, reachable],
        Duration::from_secs(1),
        |addr| {
            attempts.push(addr);
            std::future::ready(if addr == reachable {
                Ok(addr)
            } else {
                Err(io::ErrorKind::ConnectionRefused.into())
            })
        },
    )
    .await
    .unwrap();

    assert_eq!(connected, reachable);
    assert_eq!(attempts, [first, second, reachable]);
}

#[tokio::test]
async fn mail_connect_reports_the_last_distinct_address_error() {
    let first = "192.0.2.1:993".parse::<SocketAddr>().unwrap();
    let second = "192.0.2.2:993".parse::<SocketAddr>().unwrap();

    let outcome = connect_addresses([first, second, first], Duration::from_secs(1), |addr| {
        std::future::ready(if addr == second {
            Err::<(), _>(timed_out())
        } else {
            Err(io::ErrorKind::ConnectionRefused.into())
        })
    })
    .await;

    assert_eq!(outcome, Err(NetError::Timeout("timed out".to_owned())));
}
