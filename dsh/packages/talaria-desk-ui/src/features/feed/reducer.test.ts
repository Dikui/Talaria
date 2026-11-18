import { describe, expect, it } from "vitest";

import type { DeskMessage } from "./messages";
import {
  feedReducer,
  initialFeedState,
  MAX_LIVE_BARS,
  type FeedState,
} from "./reducer";
import { book, INSTRUMENT, kline } from "./testData";

function apply(state: FeedState, message: DeskMessage): FeedState {
  return feedReducer(state, { type: "message", message });
}

describe("feedReducer", () => {
  it("连接状态按 connecting → opened → closed 迁移", () => {
    let state = feedReducer(initialFeedState, { type: "connecting" });
    expect(state.connection).toBe("connecting");
    state = feedReducer(state, { type: "opened" });
    expect(state.connection).toBe("open");
    state = feedReducer(state, { type: "closed" });
    expect(state.connection).toBe("closed");
  });

  it("hello 写入 feed_mode 与 live 标记", () => {
    const state = apply(initialFeedState, {
      type: "hello",
      feed_mode: "replay",
      server_ts: 1,
      live: true,
    });
    expect(state.hello).toEqual({ feed_mode: "replay", server_ts: 1, live: true });
  });

  it("levels 帧整体替换关键位集合，不回退旧快照", () => {
    const bundle = {
      snapshot_id: "snap",
      instrument_id: INSTRUMENT,
      timeframe: "1m",
      cutoff_ts: 1,
      as_of_ts: 1,
      levels: [],
    };
    const state = apply(initialFeedState, { type: "levels", data: bundle });
    expect(state.levels).toEqual(bundle);
    expect(state.bars).toEqual([]);
    expect(state.connection).toBe("connecting");
  });

  it("同 timestamp 的 K 线原位更新，新 timestamp 追加", () => {
    let state = apply(initialFeedState, {
      type: "bar",
      data: kline(1_763_596_800_000, { revision: 0 }),
    });
    state = apply(state, {
      type: "bar",
      data: kline(1_763_596_800_000, { revision: 1, confirmed: false }),
    });
    expect(state.bars).toHaveLength(1);
    expect(state.bars[0].revision).toBe(1);
    expect(state.bars[0].confirmed).toBe(false);

    state = apply(state, {
      type: "bar",
      data: kline(1_763_596_860_000, { revision: 0 }),
    });
    expect(state.bars).toHaveLength(2);
    expect(state.bars[1].bar.timestamp).toBe(1_763_596_860_000);
  });

  it("实时 K 线缓冲封顶", () => {
    let state = initialFeedState;
    for (let index = 0; index < MAX_LIVE_BARS + 5; index += 1) {
      state = apply(state, {
        type: "bar",
        data: kline(1_763_596_800_000 + index * 60_000),
      });
    }
    expect(state.bars).toHaveLength(MAX_LIVE_BARS);
    expect(state.bars[0].bar.timestamp).toBe(
      1_763_596_800_000 + 5 * 60_000,
    );
  });

  it("盘口消息覆盖为最新一档", () => {
    let state = apply(initialFeedState, { type: "book", data: book() });
    expect(state.book?.ask_price).toBe("58001.0");
    state = apply(state, {
      type: "book",
      data: book({ ask_price: "58002.0", bid_price: "58000.5" }),
    });
    expect(state.book?.ask_price).toBe("58002.0");
  });

  it("健康消息新 ts 覆盖旧 ts，旧 ts 丢弃", () => {
    let state = apply(initialFeedState, {
      type: "health",
      data: {
        instrument_id: INSTRUMENT,
        timeframe: "1m",
        status: "LIVE",
        ts: 2_000,
        reconnect_count: 0,
      },
    });
    expect(state.health?.status).toBe("LIVE");

    state = apply(state, {
      type: "health",
      data: {
        instrument_id: INSTRUMENT,
        timeframe: "1m",
        status: "STALE",
        ts: 1_000,
        stale_since: 900,
        reconnect_count: 0,
      },
    });
    expect(state.health?.status).toBe("LIVE");

    state = apply(state, {
      type: "health",
      data: {
        instrument_id: INSTRUMENT,
        timeframe: "1m",
        status: "RECOVERING",
        ts: 3_000,
        stale_since: 2_500,
        reconnect_count: 1,
      },
    });
    expect(state.health?.status).toBe("RECOVERING");
    expect(state.health?.reconnect_count).toBe(1);
  });

  it("FEED_GAP 保留在状态中供状态栏统计", () => {
    const state = apply(initialFeedState, {
      type: "bar",
      data: kline(1_763_596_800_000, { revision: 0 }, {
        status: "FEED_GAP",
        open: null,
        high: null,
        low: null,
        close: null,
        volume: null,
        turnover: null,
      }),
    });
    expect(state.bars[0].bar.status).toBe("FEED_GAP");
    expect(state.bars[0].bar.close).toBeNull();
  });

  it("服务端错误写入 error，重连 reset 清空", () => {
    let state = apply(initialFeedState, {
      type: "bar",
      data: kline(1_763_596_800_000),
    });
    state = apply(state, {
      type: "error",
      code: "INSTRUMENT_NOT_FOUND",
      message: "未知标的",
    });
    expect(state.error).toBe("未知标的");
    state = feedReducer(state, { type: "reset" });
    expect(state.error).toBeNull();
    expect(state.bars).toEqual([]);
    expect(state.health).toBeNull();
  });

  it("切换周期后新周期健康覆盖旧周期，即使 ts 更旧", () => {
    let state = apply(initialFeedState, {
      type: "health",
      data: {
        instrument_id: INSTRUMENT,
        timeframe: "1m",
        status: "STALE",
        ts: 5_000,
        stale_since: 4_000,
        reconnect_count: 1,
      },
    });
    state = apply(state, {
      type: "health",
      data: {
        instrument_id: INSTRUMENT,
        timeframe: "5m",
        status: "LIVE",
        ts: 1_000,
        reconnect_count: 0,
      },
    });
    expect(state.health?.timeframe).toBe("5m");
    expect(state.health?.status).toBe("LIVE");
  });
});
