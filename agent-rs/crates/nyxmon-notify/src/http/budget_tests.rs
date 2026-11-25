//! Synthetic loopback peers for notifier resource bounds; no external services.
use super::*;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::task::JoinHandle;

async fn scripted(head: &[u8], chunks: Vec<(Duration, Vec<u8>)>) -> (String, JoinHandle<bool>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/", listener.local_addr().unwrap());
    let head = head.to_vec();
    let task = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut request = Vec::new();
        let mut buffer = [0u8; 1024];
        while !request.ends_with(b"fixture") {
            let count = stream.read(&mut buffer).await.unwrap();
            if count == 0 {
                return true;
            }
            request.extend_from_slice(&buffer[..count]);
        }
        if stream.write_all(&head).await.is_err() {
            return true;
        }
        for (delay, bytes) in chunks {
            tokio::time::sleep(delay).await;
            if stream.write_all(&bytes).await.is_err() {
                return true;
            }
        }
        // An owned connection driver must close even when the peer waits forever.
        matches!(stream.read(&mut buffer).await, Ok(0) | Err(_))
    });
    (url, task)
}
async fn closed(task: JoinHandle<bool>) {
    assert!(
        tokio::time::timeout(Duration::from_secs(2), task)
            .await
            .expect("connection driver closes")
            .unwrap()
    );
}
#[tokio::test]
async fn declared_and_chunked_responses_are_capped_without_echoing_content() {
    let (url, peer) = scripted(b"HTTP/1.1 200 OK\r\nContent-Length: 65537\r\n\r\n", vec![]).await;
    let client = HttpClient::new(Duration::from_secs(2));
    let err = client.post(&url, &[], "fixture".into()).await.unwrap_err();
    assert_eq!(
        err,
        TransportError::new(
            "ReadError",
            "notification response exceeds 65536 byte limit"
        )
    );
    closed(peer).await;

    let payload = [b"synthetic-private-marker".as_slice(), &vec![b'x'; 65537]].concat();
    let mut chunk = format!("{:x}\r\n", payload.len()).into_bytes();
    chunk.extend(payload);
    chunk.extend(b"\r\n0\r\n\r\n");
    let (url, peer) = scripted(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
        vec![(Duration::ZERO, chunk)],
    )
    .await;
    let err = client.post(&url, &[], "fixture".into()).await.unwrap_err();
    assert_eq!(err.class, "ReadError");
    assert!(!err.detail.contains("synthetic-private-marker"));
    closed(peer).await;
}
#[tokio::test]
async fn whole_call_deadline_stops_a_continuously_progressing_response() {
    let mut chunks = Vec::new();
    for _ in 0..20 {
        chunks.push((Duration::from_millis(50), b"1\r\nx\r\n".to_vec()));
    }
    chunks.push((Duration::ZERO, b"0\r\n\r\n".to_vec()));
    let (url, peer) = scripted(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
        chunks,
    )
    .await;
    let client = HttpClient::new(Duration::from_millis(250));
    let err = tokio::time::timeout(
        Duration::from_secs(2),
        client.post(&url, &[], "fixture".into()),
    )
    .await
    .expect("whole call is bounded")
    .unwrap_err();
    assert_eq!(err, TransportError::new("TimeoutException", "timed out"));
    closed(peer).await;
}
#[tokio::test]
async fn exact_cap_and_small_error_responses_still_succeed() {
    for (status, body) in [(200, vec![b'x'; 65536]), (400, b"synthetic error".to_vec())] {
        let header = format!(
            "HTTP/1.1 {status} X\r\nContent-Length: {}\r\n\r\n",
            body.len()
        );
        let (url, peer) = scripted(header.as_bytes(), vec![(Duration::ZERO, body.clone())]).await;
        let response = HttpClient::new(Duration::from_secs(2))
            .post(&url, &[], "fixture".into())
            .await
            .unwrap();
        assert_eq!(response.status, status);
        assert_eq!(response.body, body);
        closed(peer).await;
    }
}

#[tokio::test]
async fn caller_cancellation_closes_the_owned_connection() {
    let (url, peer) = scripted(b"", vec![]).await;
    let client = HttpClient::new(Duration::from_secs(2));
    assert!(
        tokio::time::timeout(
            Duration::from_millis(100),
            client.post(&url, &[], "fixture".into())
        )
        .await
        .is_err()
    );
    closed(peer).await;
}

#[tokio::test]
async fn one_byte_chunked_frames_use_one_bounded_response_allocation() {
    let mut wire = b"1\r\nx\r\n".repeat(4096);
    wire.extend(b"0\r\n\r\n");
    let (url, peer) = scripted(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
        vec![(Duration::ZERO, wire)],
    )
    .await;
    let response = HttpClient::new(Duration::from_secs(2))
        .post(&url, &[], "fixture".into())
        .await
        .unwrap();
    assert_eq!(response.body, vec![b'x'; 4096]);
    // The first data frame reserves the cap, so one-byte chunks do not
    // force repeated allocations.
    assert_eq!(response.body.capacity(), 65536);
    closed(peer).await;
}

#[tokio::test]
async fn empty_receipts_need_no_response_buffer() {
    for (head, tail) in [
        (
            b"HTTP/1.1 204 No Content\r\n\r\n".as_slice(),
            b"".as_slice(),
        ),
        (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n".as_slice(),
            b"0\r\nX-Test: trailer\r\n\r\n".as_slice(),
        ),
    ] {
        let (url, peer) = scripted(head, vec![(Duration::ZERO, tail.to_vec())]).await;
        let response = HttpClient::new(Duration::from_secs(2))
            .post(&url, &[], "fixture".into())
            .await
            .unwrap();
        assert!(response.body.is_empty());
        assert_eq!(response.body.capacity(), 0);
        closed(peer).await;
    }
}

#[tokio::test]
async fn mixed_chunks_share_the_response_cap() {
    let client = HttpClient::new(Duration::from_secs(2));
    for excess in [false, true] {
        let mut wire = b"1\r\nx\r\nffff\r\n".to_vec();
        wire.extend(vec![b'y'; MAX_RESPONSE_BYTES - 1]);
        wire.extend(b"\r\n");
        if excess {
            wire.extend(b"1\r\nz\r\n");
        }
        wire.extend(b"0\r\nX-Test: trailer\r\n\r\n");
        let (url, peer) = scripted(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
            vec![(Duration::ZERO, wire)],
        )
        .await;
        let response = client.post(&url, &[], "fixture".into()).await;
        if excess {
            assert_eq!(response.unwrap_err(), response_limit_error());
        } else {
            let body = response.unwrap().body;
            assert_eq!(body.len(), MAX_RESPONSE_BYTES);
            assert_eq!(body[0], b'x');
            assert!(body[1..].iter().all(|byte| *byte == b'y'));
        }
        closed(peer).await;
    }
}
