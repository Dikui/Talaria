import type {
  AlertTriggerRecord,
  DerivativeTickEvent,
  FeedHealthEvent,
  KeyLevelSet,
  KlineEvent,
  OrderBookEvent,
  ScannerTrigger,
  StructureSet,
} from "@talaria/contracts";

import type { DeskMessage } from "./messages";

export const MAX_LIVE_BARS = 600;

export type ConnectionState = "connecting" | "open" | "closed";

export interface FeedHello {
  feed_mode: string;
  server_ts: number;
  live: boolean;
}

export interface FeedState {
  connection: ConnectionState;
  hello: FeedHello | null;
  channelRealtime: boolean | null;
  health: FeedHealthEvent | null;
  book: OrderBookEvent | null;
  derivatives: DerivativeTickEvent | null;
  bars: KlineEvent[];
  levels: KeyLevelSet | null;
  structures: StructureSet | null;
  alert: AlertTriggerRecord | null;
  scanner: ScannerTrigger | null;
  deniedAlerts: string[];
  chartQuotaDenied: boolean;
  error: string | null;
}

export const initialFeedState: FeedState = {
  connection: "connecting",
  hello: null,
  channelRealtime: null,
  health: null,
  book: null,
  derivatives: null,
  bars: [],
  levels: null,
  structures: null,
  alert: null,
  scanner: null,
  deniedAlerts: [],
  chartQuotaDenied: false,
  error: null,
};

export type FeedAction =
  | { type: "connecting" }
  | { type: "opened" }
  | { type: "closed" }
  | { type: "reset" }
  | { type: "message"; message: DeskMessage };

function applyHealth(state: FeedState, health: FeedHealthEvent): FeedState {
  const current = state.health;
  if (
    current !== null &&
    current.instrument_id === health.instrument_id &&
    current.timeframe === health.timeframe &&
    health.ts < current.ts
  ) {
    return state;
  }
  return { ...state, health, chartQuotaDenied: false };
}

function applyMessage(state: FeedState, message: DeskMessage): FeedState {
  switch (message.type) {
    case "alert_quota":
      return { ...state, deniedAlerts: message.denied_alerts };
    case "hello":
      return {
        ...state,
        deniedAlerts: message.denied_alerts ?? [],
        hello: {
          feed_mode: message.feed_mode,
          server_ts: message.server_ts,
          live: message.live,
        },
        error: null,
      };
    case "channel_mode":
      return { ...state, channelRealtime: message.realtime };
    case "bar": {
      const index = state.bars.findIndex(
        (event) => event.bar.timestamp === message.data.bar.timestamp,
      );
      const bars = state.bars.slice();
      if (index >= 0) {
        bars[index] = message.data;
      } else {
        bars.push(message.data);
      }
      return { ...state, bars: bars.slice(-MAX_LIVE_BARS) };
    }
    case "book":
      return { ...state, book: message.data };
    case "derivatives":
      if (
        state.derivatives !== null &&
        state.derivatives.instrument_id === message.data.instrument_id &&
        message.data.ts_event < state.derivatives.ts_event
      ) {
        return state;
      }
      return { ...state, derivatives: message.data };
    case "levels":
      return { ...state, levels: message.data };
    case "structures":
      return { ...state, structures: message.data };
    case "alert":
      return { ...state, alert: message.data };
    case "scanner":
      return { ...state, scanner: message.data };
    case "paper_account":
      return state;
    case "health":
      return applyHealth(state, message.data);
    case "error":
      if (message.code === "SUBSCRIPTION_QUOTA" && message.owner === "chart") {
        return { ...state, health: null, chartQuotaDenied: true };
      }
      return { ...state, error: message.message };
  }
}

export function feedReducer(state: FeedState, action: FeedAction): FeedState {
  switch (action.type) {
    case "connecting":
      return { ...state, connection: "connecting" };
    case "opened":
      return { ...state, connection: "open" };
    case "closed":
      return { ...state, connection: "closed" };
    case "reset":
      return {
        ...state,
        channelRealtime: null,
        chartQuotaDenied: false,
        bars: [],
        book: null,
        derivatives: null,
        health: null,
        levels: null,
        structures: null,
        alert: null,
        scanner: null,
        error: null,
      };
    case "message":
      return applyMessage(state, action.message);
  }
}
