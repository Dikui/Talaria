import type {
  BarRange,
  MarketConfig,
  MarketDataFeed,
  OHLCV,
  SymbolInfo,
} from "@luxalgo/vela";

type Unsubscribe = () => void;
import type {
  ChartPage,
  CompactBar,
  FeedHealthEvent,
  InstrumentSpec,
  KlineEvent,
  SnapshotMeta,
} from "@talaria/contracts";

import { deskSocketHub } from "../features/feed/deskSocketHub";
import { api } from "../lib/api";
import {
  compactBarToOHLCV,
  compactBarsToVelaSeries,
  precisionToMinTick,
  rangeBarToCompactBar,
  timeframeToMs,
} from "../lib/chartData";
import type { ChartStats } from "./adapter";

export interface VelaMarketFeedClient {
  createSnapshot(
    instrumentId: string,
    timeframes: string[],
    cutoffTs?: number | null,
  ): Promise<SnapshotMeta>;
  bars(
    snapshotId: string,
    timeframe: string,
    options?: { limit?: number; beforeTs?: number },
  ): Promise<ChartPage>;
  rangeBars?: typeof api.rangeBars;
  instruments?: () => Promise<InstrumentSpec[]>;
}

export interface VelaMarketFeedOptions {
  client?: VelaMarketFeedClient;
  subscribeLiveStream?: (
    instrumentId: string,
    timeframe: string,
    onEvent: {
      onBar: (event: KlineEvent) => void;
      onHealth?: (health: FeedHealthEvent) => void;
    },
  ) => Unsubscribe;
  pageSize?: number;
  snapshotCutoff?: number | null;
  instruments?: InstrumentSpec[];
  onSnapshot?: (snapshot: SnapshotMeta | null) => void;
  onStats?: (stats: ChartStats) => void;
  onHealth?: (health: FeedHealthEvent) => void;
  onError?: (error: unknown) => void;
}

const DEFAULT_PAGE_SIZE = 300;

/**
 * MarketDataFeed implementation connecting `@luxalgo/vela` (`deps.dataFeed`) to
 * `talaria-data` HTTP snapshots/range endpoints and `/ws/desk` real-time ticks.
 *
 * Guarantees:
 * - `FEED_GAP` bars never enter `OHLCV[]` (no synthetic candles fabricated;
 *   Vela `barTimeToLogical` interpolates over actual bar timestamps).
 * - `loadRange` expands historical depth on left-edge viewport pan.
 * - Live ticks deduplicate/overwrite forming bars and count live `FEED_GAP` slots.
 */
export class VelaMarketFeed implements MarketDataFeed {
  private readonly client: VelaMarketFeedClient;
  private readonly subscribeLiveStream: NonNullable<VelaMarketFeedOptions["subscribeLiveStream"]>;
  private readonly pageSize: number;
  private snapshotCutoff: number | null;
  private instrumentsById = new Map<string, InstrumentSpec>();
  private currentSnapshot: SnapshotMeta | null = null;
  private cachedBars = new Map<number, CompactBar>();
  private liveGapTimestamps = new Set<number>();
  private historicalGapTimestamps = new Set<number>();
  private hasMoreBackward = true;
  private expanding = false;
  private onSnapshot?: (snapshot: SnapshotMeta | null) => void;
  private onStats?: (stats: ChartStats) => void;
  private onHealth?: (health: FeedHealthEvent) => void;
  private onError?: (error: unknown) => void;

  constructor(options: VelaMarketFeedOptions = {}) {
    this.client = options.client ?? api;
    this.subscribeLiveStream =
      options.subscribeLiveStream ??
      ((instrumentId, timeframe, handlers) =>
        deskSocketHub.subscribe(instrumentId, timeframe, {
          onBar: handlers.onBar,
          onHealth: handlers.onHealth,
        }));
    this.pageSize = options.pageSize ?? DEFAULT_PAGE_SIZE;
    this.snapshotCutoff = options.snapshotCutoff ?? null;
    this.onSnapshot = options.onSnapshot;
    this.onStats = options.onStats;
    this.onHealth = options.onHealth;
    this.onError = options.onError;
    if (options.instruments) {
      this.setInstruments(options.instruments);
    }
  }

  setInstruments(instruments: readonly InstrumentSpec[]): void {
    this.instrumentsById = new Map(
      instruments.map((spec) => [spec.instrument_id, spec]),
    );
  }

  setSnapshotCutoff(cutoff: number | null): void {
    this.snapshotCutoff = cutoff;
  }

  getSnapshot(): SnapshotMeta | null {
    return this.currentSnapshot;
  }

  canExpandBackward(): boolean {
    return this.hasMoreBackward && !this.expanding;
  }

  oldestLoadedTimestamp(): number | null {
    let min = Infinity;
    for (const ts of this.cachedBars.keys()) {
      if (ts < min) min = ts;
    }
    return Number.isFinite(min) ? min : null;
  }

  private emitStats(): void {
    const gapSet = new Set<number>([
      ...this.historicalGapTimestamps,
      ...this.liveGapTimestamps,
    ]);
    let loaded = 0;
    for (const bar of this.cachedBars.values()) {
      if (bar.status !== "FEED_GAP" && compactBarToOHLCV(bar) !== null) {
        loaded += 1;
      }
    }
    this.onStats?.({ loaded, gaps: gapSet.size });
  }

  private ingestCompactBars(bars: readonly CompactBar[]): OHLCV[] {
    for (const bar of bars) {
      if (bar.status === "FEED_GAP") {
        this.historicalGapTimestamps.add(bar.timestamp);
        this.cachedBars.delete(bar.timestamp);
      } else {
        this.historicalGapTimestamps.delete(bar.timestamp);
        this.cachedBars.set(bar.timestamp, bar);
      }
    }
    this.emitStats();
    const series = compactBarsToVelaSeries([...this.cachedBars.values()]);
    return series.bars;
  }

  symbolInfo(cfg: MarketConfig): SymbolInfo | undefined {
    const symbol = cfg.symbol;
    if (!symbol) return undefined;
    const spec = this.instrumentsById.get(symbol);
    const mintick = spec ? precisionToMinTick(spec.price_precision) : 0.01;
    return {
      ticker: symbol,
      timezone: "Etc/UTC",
      session: "24x7",
      mintick,
      volume: true,
      ohlc: true,
    };
  }

  async load(cfg: MarketConfig): Promise<OHLCV[]> {
    if (cfg.data) {
      return cfg.data;
    }
    const instrumentId = cfg.symbol ?? "BTC-USDT-PERP";
    const timeframe = cfg.timeframe ?? "1m";
    const limit = cfg.bars ?? this.pageSize;

    this.cachedBars.clear();
    this.historicalGapTimestamps.clear();
    this.liveGapTimestamps.clear();
    this.hasMoreBackward = true;

    try {
      const meta = await this.client.createSnapshot(
        instrumentId,
        [timeframe],
        this.snapshotCutoff,
      );
      this.currentSnapshot = meta;
      this.onSnapshot?.(meta);

      const page = await this.client.bars(meta.snapshot_id, timeframe, { limit });
      this.hasMoreBackward = page.has_more_backward;
      return this.ingestCompactBars(page.bars);
    } catch (error) {
      this.currentSnapshot = null;
      this.onSnapshot?.(null);
      this.onError?.(error);
      throw error;
    }
  }

  async loadRange(cfg: MarketConfig, range: BarRange): Promise<OHLCV[]> {
    const instrumentId = cfg.symbol ?? "BTC-USDT-PERP";
    const timeframe = cfg.timeframe ?? "1m";
    const limit = range.limit ?? this.pageSize;

    if (range.to !== undefined && this.currentSnapshot) {
      this.expanding = true;
      try {
        const page = await this.client.bars(this.currentSnapshot.snapshot_id, timeframe, {
          limit,
          beforeTs: range.to,
        });
        this.hasMoreBackward = page.has_more_backward;
        const series = compactBarsToVelaSeries(page.bars);
        this.ingestCompactBars(page.bars);
        return series.bars;
      } catch (error) {
        this.onError?.(error);
        return [];
      } finally {
        this.expanding = false;
      }
    }

    if (range.from !== undefined && this.client.rangeBars) {
      try {
        const res = await this.client.rangeBars({
          instrument: instrumentId,
          timeframe,
          start: range.from,
          end: range.to ?? null,
          as_of: this.snapshotCutoff,
          include_open: this.snapshotCutoff === null,
        });
        const compact = res.bars.map(rangeBarToCompactBar);
        const series = compactBarsToVelaSeries(compact);
        this.ingestCompactBars(compact);
        return series.bars;
      } catch {
        // Fall through to cached bars / snapshot tail.
      }
    }

    const tfMs = timeframeToMs(timeframe);
    const fromBound = range.from ?? 0;
    const toBound = range.to ?? Date.now() + tfMs;
    const series = compactBarsToVelaSeries([...this.cachedBars.values()]);
    return series.bars.filter((bar) => bar.time >= fromBound && bar.time <= toBound);
  }

  subscribe(cfg: MarketConfig, onBar: (bar: OHLCV) => void): Unsubscribe {
    if (this.snapshotCutoff !== null || cfg.data) {
      return () => undefined;
    }
    const instrumentId = cfg.symbol ?? "BTC-USDT-PERP";
    const timeframe = cfg.timeframe ?? "1m";

    return this.subscribeLiveStream(instrumentId, timeframe, {
      onHealth: (health) => {
        this.onHealth?.(health);
      },
      onBar: (event) => {
        if (event.bar.status === "FEED_GAP") {
          this.liveGapTimestamps.add(event.bar.timestamp);
          this.emitStats();
          return;
        }
        this.liveGapTimestamps.delete(event.bar.timestamp);
        this.cachedBars.set(event.bar.timestamp, event.bar);
        this.emitStats();
        const point = compactBarToOHLCV(event.bar);
        if (point !== null) {
          onBar(point);
        }
      },
    });
  }

  destroy(): void {
    this.cachedBars.clear();
    this.historicalGapTimestamps.clear();
    this.liveGapTimestamps.clear();
  }
}
