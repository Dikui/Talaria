import type { OHLCV } from "@luxalgo/vela";
import type { CompactBar, RangeBar } from "@talaria/contracts";

/**
 * DecimalString -> number occurs ONLY at the final render boundary.
 * FEED_GAP bars carry no OHLC prices and are NEVER fabricated into fake candles;
 * Vela's `barTimeToLogical()` interpolates across real timestamps seamlessly.
 */
export function compactBarToOHLCV(bar: CompactBar): OHLCV | null {
  if (
    bar.status === "FEED_GAP" ||
    bar.open === null ||
    bar.high === null ||
    bar.low === null ||
    bar.close === null
  ) {
    return null;
  }
  const open = Number(bar.open);
  const high = Number(bar.high);
  const low = Number(bar.low);
  const close = Number(bar.close);
  if (
    !Number.isFinite(open) ||
    !Number.isFinite(high) ||
    !Number.isFinite(low) ||
    !Number.isFinite(close)
  ) {
    return null;
  }
  const volume = bar.volume === null ? 0 : Number(bar.volume);
  return {
    time: bar.timestamp,
    open,
    high,
    low,
    close,
    volume: Number.isFinite(volume) ? volume : 0,
  };
}

export function rangeBarToCompactBar(bar: RangeBar): CompactBar {
  return {
    timestamp: bar.ts_event,
    open: bar.open,
    high: bar.high,
    low: bar.low,
    close: bar.close,
    volume: bar.volume,
    turnover: bar.turnover,
    status: "NORMAL",
    ts_available: bar.ts_available,
    precision: bar.precision,
  };
}

export interface VelaBarSeries {
  bars: OHLCV[];
  gapCount: number;
  gapTimestamps: number[];
}

/**
 * Convert CompactBar[] into strictly ascending, deduplicated OHLCV[] for Vela,
 * skipping all FEED_GAP slots without fabricating synthetic bars.
 */
export function compactBarsToVelaSeries(rawBars: readonly CompactBar[]): VelaBarSeries {
  const sorted = [...rawBars].sort((a, b) => a.timestamp - b.timestamp);
  const byTime = new Map<number, OHLCV>();
  const gapSet = new Set<number>();

  for (const bar of sorted) {
    if (bar.status === "FEED_GAP") {
      gapSet.add(bar.timestamp);
      byTime.delete(bar.timestamp);
      continue;
    }
    const point = compactBarToOHLCV(bar);
    if (point !== null) {
      gapSet.delete(bar.timestamp);
      byTime.set(bar.timestamp, point);
    }
  }

  return {
    bars: [...byTime.values()].sort((a, b) => a.time - b.time),
    gapCount: gapSet.size,
    gapTimestamps: [...gapSet].sort((a, b) => a - b),
  };
}

export function timeframeToMs(timeframe: string): number {
  const match = /^(\d+)([smhdw])$/i.exec(timeframe.trim());
  if (!match) return 60_000;
  const count = Number(match[1]);
  switch (match[2].toLowerCase()) {
    case "s":
      return count * 1_000;
    case "m":
      return count * 60_000;
    case "h":
      return count * 3_600_000;
    case "d":
      return count * 86_400_000;
    case "w":
      return count * 7 * 86_400_000;
    default:
      return 60_000;
  }
}

export function precisionToMinTick(pricePrecision: number): number {
  const digits = Math.min(8, Math.max(0, Math.trunc(pricePrecision)));
  return 10 ** -digits;
}
