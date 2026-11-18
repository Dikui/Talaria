// 本文件由 `uv run talaria contract-check --write` 生成，请勿手改。

export type DecimalString = string;

export type PrecisionTier = "SECOND_VERIFIED" | "SESSION_BOUND" | "DATE_CONSERVATIVE";

export type BarStatus = "NORMAL" | "NO_TRADES" | "FEED_GAP";

export type FeedHealthStatus = "LIVE" | "STALE" | "RECOVERING";

export type SnapshotMode = "HISTORICAL_PIT" | "OBSERVED_REPLAY";

export interface TemporalStamp {
  ts_event: number;
  ts_published?: number | null;
  ts_available: number;
  precision: PrecisionTier;
  ts_ingested: number;
  ts_committed: number;
}
export interface InstrumentSpec {
  instrument_id: string;
  venue: string;
  symbol: string;
  asset_class: string;
  tick_size: DecimalString;
  qty_step: DecimalString;
  min_qty: DecimalString;
  price_precision: number;
  qty_precision: number;
}
export interface CompactBar {
  timestamp: number;
  open: DecimalString | null;
  high: DecimalString | null;
  low: DecimalString | null;
  close: DecimalString | null;
  volume: DecimalString | null;
  turnover: DecimalString | null;
  status: BarStatus;
  ts_available: number;
  precision: PrecisionTier;
}
export interface KlineEvent {
  instrument_id: string;
  timeframe: string;
  bar: CompactBar;
  confirmed: boolean;
  revision: number;
}
export interface OrderBookEvent {
  instrument_id: string;
  bid_price: DecimalString;
  bid_size: DecimalString;
  ask_price: DecimalString;
  ask_size: DecimalString;
  ts_event: number;
  ts_ingested: number;
}
export interface FeedHealthEvent {
  instrument_id: string;
  timeframe: string;
  status: FeedHealthStatus;
  ts: number;
  last_event_ts?: number | null;
  stale_since?: number | null;
  reconnect_count?: number;
}
export interface FeedHealthReport {
  feed_mode: string;
  status: FeedHealthStatus;
  channels: FeedHealthEvent[];
}
export interface SnapshotRequest {
  instrument_id: string;
  timeframes: string[];
  cutoff_ts?: number | null;
  mode?: SnapshotMode;
}
export interface SnapshotMeta {
  snapshot_id: string;
  instrument_id: string;
  timeframes: string[];
  cutoff_ts: number;
  created_ts: number;
  mode: SnapshotMode;
  bar_counts: Record<string, number>;
}
export interface ChartPage {
  snapshot_id: string;
  instrument_id: string;
  timeframe: string;
  bars: CompactBar[];
  has_more_forward: boolean;
  has_more_backward: boolean;
}
export interface ErrorResponse {
  code: string;
  message: string;
}
