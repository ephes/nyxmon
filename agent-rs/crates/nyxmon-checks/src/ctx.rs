//! Per-batch execution context of the executors.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::http_client::HttpSession;

/// `anyio.sleep(seconds)` for retry delays; tests record the delays instead
/// of waiting.
#[derive(Clone, Debug, Default)]
pub struct Sleeper {
    recorded: Option<Arc<Mutex<Vec<f64>>>>,
}

impl Sleeper {
    /// A sleeper that waits on the tokio clock.
    pub fn real() -> Self {
        Sleeper::default()
    }

    /// A sleeper that returns at once and records every requested delay.
    pub fn recording() -> (Self, Arc<Mutex<Vec<f64>>>) {
        let recorded = Arc::new(Mutex::new(Vec::new()));
        (
            Sleeper {
                recorded: Some(recorded.clone()),
            },
            recorded,
        )
    }

    /// Sleep `seconds` like `anyio.sleep`: zero, negative and NaN delays
    /// only yield (asyncio runs a NaN timer at once), an infinite or
    /// unrepresentable delay never ends.
    pub async fn sleep(&self, seconds: f64) {
        if let Some(recorded) = &self.recorded {
            recorded
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .push(seconds);
            return;
        }
        if seconds.is_nan() || seconds <= 0.0 {
            tokio::task::yield_now().await;
            return;
        }
        match Duration::try_from_secs_f64(seconds) {
            Ok(duration) => tokio::time::sleep(duration).await,
            Err(_) => std::future::pending::<()>().await,
        }
    }
}

/// What one batch shares (`_async_run_batch`): the HTTP session of the
/// HTTP-family checks and the sleeper for retry delays.
pub struct BatchCtx {
    pub http: HttpSession,
    pub sleeper: Sleeper,
}

impl BatchCtx {
    pub fn new() -> Self {
        BatchCtx {
            http: HttpSession::new(),
            sleeper: Sleeper::real(),
        }
    }
}

impl Default for BatchCtx {
    fn default() -> Self {
        BatchCtx::new()
    }
}
