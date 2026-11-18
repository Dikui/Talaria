import { describe, expect, it } from "vitest";

import type { KeyLevel } from "@talaria/contracts";

import {
  defaultKeyLevelFilter,
  formatLevelPrice,
  layoutKeyLevelLabels,
  levelLabel,
  levelPrice,
  toggleCategory,
  visibleKeyLevels,
} from "./model";

function level(overrides: Partial<KeyLevel> = {}): KeyLevel {
  return {
    level_id: "kl_test",
    instrument_id: "BTC-USDT-PERP",
    kind: "SWING_HIGH",
    category: "STRUCTURE",
    timeframe: "1m",
    price: "120.00",
    strength: 0.7,
    status: "ACTIVE",
    ts_available: 1,
    formed_ts: 1,
    method_version: "key-levels@1",
    parameters: {},
    touch_count: 2,
    last_touch_ts: 1,
    volume_weight: "0",
    ...overrides,
  };
}

describe("visibleKeyLevels", () => {
  it("按类别与最低强度过滤，并移除已忽略项", () => {
    const levels = [
      level({ level_id: "a", strength: 0.8 }),
      level({ level_id: "b", category: "SESSION", kind: "PREV_DAY_HIGH", price: "118.00", strength: 0.5 }),
      level({ level_id: "c", strength: 0.2 }),
    ];
    const visible = visibleKeyLevels(
      levels,
      { categories: ["STRUCTURE"], minStrength: 0.35 },
      new Set(["a"]),
    );
    expect(visible.map((item) => item.level_id)).toEqual([]);

    const all = visibleKeyLevels(levels, defaultKeyLevelFilter, new Set());
    expect(all.map((item) => item.level_id)).toEqual(["a", "b"]);
  });

  it("切换类别后保持其余筛选条件", () => {
    const next = toggleCategory(
      { categories: ["STRUCTURE", "SESSION"], minStrength: 0.4 },
      "SESSION",
    );
    expect(next.categories).toEqual(["STRUCTURE"]);
    expect(next.minStrength).toBe(0.4);
  });
});

describe("levels formatting", () => {
  it("区间位使用中点投影、显示 low–high", () => {
    const zone = level({
      level_id: "zone",
      price: undefined,
      price_low: "120.20",
      price_high: "120.60",
      kind: "OPENING_RANGE",
      category: "SESSION",
    });
    expect(levelPrice(zone)).toBeCloseTo(120.4);
    expect(formatLevelPrice(zone, 2)).toBe("120.20–120.60");
    expect(levelLabel(zone, 2)).toBe("开盘区间 120.20–120.60");
  });

  it("非 ACTIVE 状态体现在长标签里", () => {
    expect(levelLabel(level({ status: "FLIPPED" }), 1)).toBe(
      "摆动高点 120.0 · 反转",
    );
  });
});

describe("layoutKeyLevelLabels", () => {
  const frame = { width: 900, height: 420, top: 10, bottom: 400 };

  it("低点靠近时依次避让，且全部留在 pane 内", () => {
    const placed = layoutKeyLevelLabels(
      [
        { id: "a", text: "摆动高点 120.00", y: 12 },
        { id: "b", text: "前日高 119.50", y: 16 },
        { id: "c", text: "缺口 118.00", y: 20 },
      ],
      frame,
    );
    expect(placed).toHaveLength(3);
    for (let index = 1; index < placed.length; index += 1) {
      expect(placed[index].y).toBeGreaterThanOrEqual(
        placed[index - 1].y + placed[index - 1].height,
      );
    }
    for (const label of placed) {
      expect(label.y).toBeGreaterThanOrEqual(frame.top + 8);
      expect(label.y + label.height).toBeLessThanOrEqual(frame.bottom - 8);
      expect(label.x).toBeGreaterThanOrEqual(8);
      expect(label.x - label.width).toBeGreaterThanOrEqual(8);
    }
  });

  it("超长文本省略且不越界", () => {
    const placed = layoutKeyLevelLabels(
      [{ id: "long", text: "开盘区间 120.20–120.60 · 反转 · 已确认 · 成交量加权", y: 200 }],
      frame,
    );
    expect(placed[0].text.endsWith("…")).toBe(true);
    expect(placed[0].width).toBeLessThanOrEqual(240);
    expect(placed[0].text.length).toBeLessThan(30);
  });

  it("窄图贴左、宽图贴右，rightInset 让位给过滤面板", () => {
    const narrow = layoutKeyLevelLabels(
      [{ id: "n", text: "摆动高点 120.00", y: 100 }],
      { width: 360, height: 300, top: 0, bottom: 300 },
    );
    expect(narrow[0].anchor).toBe("start");
    expect(narrow[0].x).toBe(8);

    const wide = layoutKeyLevelLabels(
      [{ id: "w", text: "摆动高点 120.00", y: 100 }],
      frame,
    );
    expect(wide[0].anchor).toBe("end");
    expect(wide[0].x).toBe(frame.width - 8);

    const inset = layoutKeyLevelLabels(
      [{ id: "i", text: "摆动高点 120.00", y: 100 }],
      { ...frame, rightInset: 260 },
    );
    expect(inset[0].x).toBe(frame.width - 8 - 260);
  });

  it("空输入返回空，乱序输入按 y 稳定排序", () => {
    expect(layoutKeyLevelLabels([], frame)).toEqual([]);
    const placed = layoutKeyLevelLabels(
      [
        { id: "bottom", text: "底", y: 300 },
        { id: "top", text: "顶", y: 100 },
      ],
      frame,
    );
    expect(placed.map((item) => item.id)).toEqual(["top", "bottom"]);
  });
});
