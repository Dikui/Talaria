import type {
  FeedHealthEvent,
  KlineEvent,
  OrderBookEvent,
} from "@talaria/contracts";

import type { DeskMessage } from "./messages";

/** 前端保留的实时推送上限；图表只需要最近的增量，历史仍由快照提供。 */
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
  health: FeedHealthEvent | null;
  book: OrderBookEvent | null;
  bars: KlineEvent[];
  error: string | null;
}

export const initialFeedState: FeedState = {
  connection: "connecting",
  hello: null,
  health: null,
  book: null,
  bars: [],
  error: null,
};

export type FeedAction =
  | { type: "connecting" }
  | { type: "opened" }
  | { type: "closed" }
  | { type: "reset" }
  | { type: "message"; message: DeskMessage };

function applyHealth(
  state: FeedState,
  health: FeedHealthEvent,
): FeedState {
  const current = state.health;
  if (
    current !== null &&
    current.instrument_id === health.instrument_id &&
    health.ts < current.ts
  ) {
    return state;
  }
  return { ...state, health };
}

function applyMessage(state: FeedState, message: DeskMessage): FeedState {
  switch (message.type) {
    case "hello": {
      return {
        ...state,
        hello: {
          feed_mode: message.feed_mode,
          server_ts: message.server_ts,
          live: message.live,
        },
        error: null,
      };
    }
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
    case "health":
      return applyHealth(state, message.data);
    case "error":
      return { ...state, error: message.message };
  }
}

/** 纯 reducer：连接状态 + 服务端增量，便于确定性测试。 */
export function feedReducer(state: FeedState, action: FeedAction): FeedState {
  switch (action.type) {
    case "connecting":
      return { ...state, connection: "connecting" };
    case "opened":
      return { ...state, connection: "open" };
    case "closed":
      return { ...state, connection: "closed" };
    case "reset":
      return { ...state, bars: [], book: null, health: null, error: null };
    case "message":
      return applyMessage(state, action.message);
  }
}
