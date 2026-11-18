import type { CompactBar, KlineEvent, OrderBookEvent } from "@talaria/contracts";

export const INSTRUMENT = "BTC-USDT-PERP";
export const BASE = 1_763_596_800_000;
export const MINUTE = 60_000;

export function bar(timestamp: number, overrides: Partial<CompactBar> = {}): CompactBar {
  return {
    timestamp,
    open: "100.0",
    high: "101.0",
    low: "99.0",
    close: "100.5",
    volume: "1.5",
    turnover: "150.0",
    status: "NORMAL",
    ts_available: timestamp + MINUTE,
    precision: "SECOND_VERIFIED",
    ...overrides,
  };
}

export function kline(
  timestamp: number,
  overrides: Partial<KlineEvent> = {},
  barOverrides: Partial<CompactBar> = {},
): KlineEvent {
  return {
    instrument_id: INSTRUMENT,
    timeframe: "1m",
    bar: bar(timestamp, barOverrides),
    confirmed: true,
    revision: 0,
    ...overrides,
  };
}

export function book(overrides: Partial<OrderBookEvent> = {}): OrderBookEvent {
  return {
    instrument_id: INSTRUMENT,
    bid_price: "58000.0",
    bid_size: "1.2",
    ask_price: "58001.0",
    ask_size: "0.8",
    ts_event: BASE,
    ts_ingested: BASE,
    ...overrides,
  };
}
