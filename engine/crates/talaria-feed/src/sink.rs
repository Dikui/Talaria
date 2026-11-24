//! Decoupled persistence sink trait invoked by `FeedSupervisor` without depending on `talaria-store`.

use talaria_contracts::{CompactBar, FeedEvent, InstrumentSpec, RawBar};
use talaria_packs::TransportFrame;

/// Sink receiving confirmed bar closes, REST-recovered bars, subscriptions, and raw feed events.
///
/// Wave 3 `talaria-server` implements this trait to wire `FeedSupervisor` into `talaria-store`
/// without creating a crate dependency from `talaria-feed` to `talaria-store`.
pub trait BarPublishSink: Send + Sync {
    /// Invoked when a live channel subscribes so downstream can schedule initial history backfill.
    fn on_subscribe(&self, instrument: &InstrumentSpec, timeframe: &str, subscribed_ts: i64) {
        let _ = (instrument, timeframe, subscribed_ts);
    }

    /// Invoked on confirmed live bar close (and idempotent re-confirmations for retry resilience).
    fn on_bar_close(
        &self,
        instrument: &InstrumentSpec,
        timeframe: &str,
        bar: &CompactBar,
        received_ts: i64,
        revision: u64,
    ) {
        let _ = (instrument, timeframe, bar, received_ts, revision);
    }

    /// Invoked for each REST-recovered historical bar during outage recovery (`history_only` path).
    fn on_recovered_bar(&self, instrument: &InstrumentSpec, bar: &RawBar) {
        let _ = (instrument, bar);
    }

    /// Invoked for every emitted [`FeedEvent`] (kline, book, derivatives, trade, health).
    fn on_event(&self, event: &FeedEvent) {
        let _ = event;
    }

    /// Optional hook receiving transport metadata frames (`ReceivedOrderBookEvent`, `ReceivedTradeEvent`).
    fn on_received_frame(&self, frame: &TransportFrame) {
        let _ = frame;
    }
}

/// Default no-op [`BarPublishSink`] for standalone testing or read-only feed usage.
#[derive(Debug, Default, Clone, Copy)]
pub struct NoopBarPublishSink;

impl BarPublishSink for NoopBarPublishSink {}
