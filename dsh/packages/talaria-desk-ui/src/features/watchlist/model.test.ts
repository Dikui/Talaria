import { describe, expect, it } from "vitest";

import type { WatchlistItem } from "@talaria/contracts";

import {
  canEditGroup,
  isEmptyWatchlist,
  isInstrumentSelected,
  moveId,
  quoteView,
} from "./model";

function item(overrides: Partial<WatchlistItem> = {}): WatchlistItem {
  return {
    instrument_id: "1000BONK-USDT-PERP",
    position: 0,
    latest_price: "0.023560",
    change_pct: "1.25",
    quote_ts: 1_795_022_974_103,
    quote_source: "SNAPSHOT",
    realtime: false,
    feed_status: null,
    stale: false,
    degraded: false,
    degradation_reason: null,
    ...overrides,
  };
}

describe("Watchlist 排序", () => {
  it("上移和下移交换相邻 id", () => {
    expect(moveId(["a", "b", "c"], "b", -1)).toEqual(["b", "a", "c"]);
    expect(moveId(["a", "b", "c"], "b", 1)).toEqual(["a", "c", "b"]);
  });

  it("越界、未知 id 与单元素返回原顺序副本", () => {
    const original = ["a", "b"];
    const edge = moveId(original, "a", -1);
    expect(edge).toEqual(["a", "b"]);
    expect(edge).not.toBe(original);
    expect(moveId(original, "missing", 1)).toEqual(["a", "b"]);
    expect(moveId(["a"], "a", 1)).toEqual(["a"]);
  });
});

describe("Watchlist 空态与切换", () => {
  it("只有加载完成且分组为空才进入空态引导", () => {
    expect(isEmptyWatchlist(null)).toBe(false);
    expect(isEmptyWatchlist([])).toBe(true);
    expect(isEmptyWatchlist([{ items: [] }])).toBe(false);
  });

  it("点击切换后只选中目标标的", () => {
    expect(isInstrumentSelected("ETH-USDT-PERP", "ETH-USDT-PERP")).toBe(true);
    expect(isInstrumentSelected("BTC-USDT-PERP", "ETH-USDT-PERP")).toBe(false);
  });
});

describe("Watchlist 响应式只读", () => {
  const manual = { is_system: false, kind: "MANUAL" as const };

  it("390 宽度隐藏编辑，1440 宽度可编辑", () => {
    expect(canEditGroup(390, manual)).toBe(false);
    expect(canEditGroup(720, manual)).toBe(false);
    expect(canEditGroup(721, manual)).toBe(true);
    expect(canEditGroup(1440, manual)).toBe(true);
  });

  it("系统动态分组在所有宽度只读", () => {
    expect(
      canEditGroup(1440, { is_system: true, kind: "SCANNER_FOCUS" }),
    ).toBe(false);
    expect(
      canEditGroup(1440, { is_system: false, kind: "SCANNER_FOCUS" }),
    ).toBe(false);
    expect(
      canEditGroup(390, { is_system: true, kind: "MANUAL" }),
    ).toBe(false);
  });
});

describe("Watchlist 报价与状态标签", () => {
  it("实时来源展示实时流与涨跌", () => {
    const view = quoteView(
      item({
        quote_source: "SUPERVISOR",
        realtime: true,
        latest_price: "68685.3",
        change_pct: "10.00",
        feed_status: "LIVE",
      }),
      1,
    );
    expect(view.price).toBe("68685.3");
    expect(view.change).toBe("+10.00%");
    expect(view.changeTone).toBe("up");
    expect(view.statusLabel).toBe("实时");
    expect(view.sourceLabel).toBe("实时流");
    expect(view.staleLabel).toBeNull();
  });

  it("配额不足明确标注配额降级", () => {
    const view = quoteView(
      item({
        realtime: false,
        degraded: true,
        degradation_reason: "QUOTA",
      }),
      6,
    );
    expect(view.statusLabel).toBe("配额降级");
    expect(view.degradationLabel).toBe("配额降级");
    expect(view.price).toBe("0.023560");
  });

  it("无实时源、无报价与 STALE 都有明确标签", () => {
    const noFeed = quoteView(
      item({ degraded: true, degradation_reason: "NO_FEED" }),
      2,
    );
    expect(noFeed.statusLabel).toBe("无实时源");

    const unavailable = quoteView(
      item({
        quote_source: "UNAVAILABLE",
        latest_price: null,
        change_pct: null,
        degraded: true,
        degradation_reason: "NO_FEED",
      }),
      2,
    );
    expect(unavailable.price).toBe("—");
    expect(unavailable.change).toBe("—");
    expect(unavailable.changeTone).toBe("flat");
    expect(unavailable.sourceLabel).toBe("无报价");

    const stale = quoteView(
      item({
        feed_status: "STALE",
        stale: true,
        latest_price: "100.5",
        change_pct: "-1.234",
      }),
      1,
    );
    expect(stale.staleLabel).toBe("STALE");
    expect(stale.change).toBe("-1.23%");
    expect(stale.changeTone).toBe("down");
  });
});
