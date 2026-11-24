//! Injectable clocks and async sleep primitives so deterministic tests never sleep real time.

use std::sync::Mutex;
use std::sync::atomic::{AtomicI64, Ordering};
use std::time::{Duration, Instant};

use talaria_packs::BoxFuture;
use tokio::sync::Notify;

/// Clock abstraction supplying UTC wall time, monotonic elapsed ms, and async sleep.
pub trait FeedClock: Send + Sync {
    /// Current UTC epoch time in milliseconds.
    fn now_ms(&self) -> i64;

    /// Monotonic elapsed milliseconds used for relative replay offset pacing.
    fn elapsed_ms(&self) -> i64 {
        self.now_ms()
    }

    /// Asynchronously wait for `duration`.
    fn sleep(&self, duration: Duration) -> BoxFuture<'_, ()>;
}

/// Real wall/Tokio clock used in production.
#[derive(Debug)]
pub struct SystemClock {
    started: Instant,
}

impl SystemClock {
    pub fn new() -> Self {
        Self {
            started: Instant::now(),
        }
    }
}

impl Default for SystemClock {
    fn default() -> Self {
        Self::new()
    }
}

impl FeedClock for SystemClock {
    fn now_ms(&self) -> i64 {
        chrono::Utc::now().timestamp_millis()
    }

    fn elapsed_ms(&self) -> i64 {
        i64::try_from(self.started.elapsed().as_millis()).unwrap_or(i64::MAX)
    }

    fn sleep(&self, duration: Duration) -> BoxFuture<'_, ()> {
        Box::pin(tokio::time::sleep(duration))
    }
}

/// Fast-forwarding virtual clock: `sleep(d)` records `d` and advances virtual time immediately.
#[derive(Debug)]
pub struct StepClock {
    wall_ms: AtomicI64,
    elapsed_ms: AtomicI64,
    sleeps: Mutex<Vec<Duration>>,
}

impl StepClock {
    /// Create a `StepClock` starting at `initial_wall_ms` with `elapsed_ms = 0`.
    pub fn new(initial_wall_ms: i64) -> Self {
        Self {
            wall_ms: AtomicI64::new(initial_wall_ms),
            elapsed_ms: AtomicI64::new(0),
            sleeps: Mutex::new(Vec::new()),
        }
    }

    pub fn set_wall_ms(&self, wall_ms: i64) -> i64 {
        self.wall_ms.store(wall_ms, Ordering::SeqCst);
        wall_ms
    }

    pub fn advance_ms(&self, delta_ms: i64) -> i64 {
        self.elapsed_ms.fetch_add(delta_ms, Ordering::SeqCst);
        self.wall_ms.fetch_add(delta_ms, Ordering::SeqCst) + delta_ms
    }

    pub fn sleeps(&self) -> Vec<Duration> {
        self.sleeps.lock().expect("step clock lock").clone()
    }
}

impl FeedClock for StepClock {
    fn now_ms(&self) -> i64 {
        self.wall_ms.load(Ordering::SeqCst)
    }

    fn elapsed_ms(&self) -> i64 {
        self.elapsed_ms.load(Ordering::SeqCst)
    }

    fn sleep(&self, duration: Duration) -> BoxFuture<'_, ()> {
        Box::pin(async move {
            if !duration.is_zero() {
                self.sleeps.lock().expect("step clock lock").push(duration);
                let ms = i64::try_from(duration.as_millis()).unwrap_or(i64::MAX);
                self.wall_ms.fetch_add(ms, Ordering::SeqCst);
                self.elapsed_ms.fetch_add(ms, Ordering::SeqCst);
            }
            tokio::task::yield_now().await;
        })
    }
}

/// Manually controlled virtual clock where `sleep` blocks until `advance_ms` / `set_wall_ms` reaches target.
#[derive(Debug)]
pub struct ManualClock {
    wall_ms: AtomicI64,
    elapsed_ms: AtomicI64,
    sleeps: Mutex<Vec<Duration>>,
    notify: Notify,
}

impl ManualClock {
    pub fn new(initial_wall_ms: i64) -> Self {
        Self {
            wall_ms: AtomicI64::new(initial_wall_ms),
            elapsed_ms: AtomicI64::new(0),
            sleeps: Mutex::new(Vec::new()),
            notify: Notify::new(),
        }
    }

    pub fn set_wall_ms(&self, wall_ms: i64) -> i64 {
        let prev = self.wall_ms.swap(wall_ms, Ordering::SeqCst);
        self.elapsed_ms.fetch_add(wall_ms - prev, Ordering::SeqCst);
        self.notify.notify_waiters();
        wall_ms
    }

    pub fn advance_ms(&self, delta_ms: i64) -> i64 {
        self.elapsed_ms.fetch_add(delta_ms, Ordering::SeqCst);
        let next = self.wall_ms.fetch_add(delta_ms, Ordering::SeqCst) + delta_ms;
        self.notify.notify_waiters();
        next
    }

    pub fn sleeps(&self) -> Vec<Duration> {
        self.sleeps.lock().expect("manual clock lock").clone()
    }
}

impl FeedClock for ManualClock {
    fn now_ms(&self) -> i64 {
        self.wall_ms.load(Ordering::SeqCst)
    }

    fn elapsed_ms(&self) -> i64 {
        self.elapsed_ms.load(Ordering::SeqCst)
    }

    fn sleep(&self, duration: Duration) -> BoxFuture<'_, ()> {
        Box::pin(async move {
            if duration.is_zero() {
                return;
            }
            self.sleeps.lock().expect("manual clock lock").push(duration);
            let delta = i64::try_from(duration.as_millis()).unwrap_or(i64::MAX);
            let target = self.elapsed_ms.load(Ordering::SeqCst) + delta;
            loop {
                let notified = self.notify.notified();
                if self.elapsed_ms.load(Ordering::SeqCst) >= target {
                    return;
                }
                notified.await;
            }
        })
    }
}
