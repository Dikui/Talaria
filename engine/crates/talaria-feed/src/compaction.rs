//! Compact historical `RawBar` slices into gap-aware `CompactBar` sequences.

use std::collections::BTreeMap;

use talaria_contracts::{
    BarStatus, CompactBar, RawBar, classify_flat_bar, timeframe_ms,
};

use crate::error::FeedError;

/// Maximum consecutive missing slots expanded into `FEED_GAP` bars for one gap.
pub const MAX_GAP_FILL: usize = 1000;

/// Deduplicate `bars` by `ts_event` (last-writer wins) at `ts_available <= cutoff_ts`,
/// insert `FEED_GAP` slots across gaps up to [`MAX_GAP_FILL`], and classify `NO_TRADES`.
pub fn compact_series(
    bars: &[RawBar],
    timeframe: &str,
    cutoff_ts: i64,
) -> Result<Vec<CompactBar>, FeedError> {
    let interval = timeframe_ms(timeframe)?;
    let mut deduped: BTreeMap<i64, &RawBar> = BTreeMap::new();
    for bar in bars {
        if bar.ts_available <= cutoff_ts {
            deduped.insert(bar.ts_event, bar);
        }
    }

    let mut compact = Vec::with_capacity(deduped.len());
    let mut previous_ts: Option<i64> = None;
    for bar in deduped.into_values() {
        if let Some(prev) = previous_ts {
            let missing = (bar.ts_event - prev) / interval - 1;
            if missing > 0 && (missing as usize) <= MAX_GAP_FILL {
                for step in 1..=missing {
                    let gap_ts = prev + step * interval;
                    compact.push(CompactBar {
                        timestamp: gap_ts,
                        open: None,
                        high: None,
                        low: None,
                        close: None,
                        volume: None,
                        turnover: None,
                        status: BarStatus::FeedGap,
                        ts_available: gap_ts + interval,
                        precision: bar.precision,
                    });
                }
            }
        }
        let status = classify_flat_bar(bar.open, bar.high, bar.low, bar.close, bar.volume);
        compact.push(bar.to_compact(status)?);
        previous_ts = Some(bar.ts_event);
    }

    Ok(compact)
}
