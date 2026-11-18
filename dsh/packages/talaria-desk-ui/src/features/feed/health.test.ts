import { describe, expect, it } from "vitest";

import {
  formatDuration,
  healthLabel,
  healthTone,
  isStaleWindowExceeded,
  staleDurationMs,
  STALE_AFTER_MS,
} from "./health";
import type { FeedHealthEvent } from "@talaria/contracts";

const INSTRUMENT = "BTC-USDT-PERP";

function health(overrides: Partial<FeedHealthEvent> = {}): FeedHealthEvent {
  return {
    instrument_id: INSTRUMENT,
    status: "LIVE",
    ts: 1_000,
    reconnect_count: 0,
    ...overrides,
  };
}

describe("健康计时纯函数", () => {
  it("STALE 的断流时长从 stale_since 起算", () => {
    const stale = health({ status: "STALE", stale_since: 1_000, ts: 1_000 });
    expect(staleDurationMs(stale, 9_000)).toBe(8_000);
    expect(staleDurationMs(stale, 500)).toBe(0);
  });

  it("非 STALE 或缺少 stale_since 时为零", () => {
    expect(staleDurationMs(null, 10_000)).toBe(0);
    expect(staleDurationMs(health({ status: "LIVE" }), 10_000)).toBe(0);
    expect(staleDurationMs(health({ status: "RECOVERING" }), 10_000)).toBe(0);
  });

  it("时长格式化为简体中文", () => {
    expect(formatDuration(0)).toBe("0 秒");
    expect(formatDuration(8_400)).toBe("8 秒");
    expect(formatDuration(120_000)).toBe("2 分");
    expect(formatDuration(125_000)).toBe("2 分 5 秒");
  });

  it("徽标文案区分三态并展示断流秒数", () => {
    const now = 1_763_596_808_000;
    expect(healthLabel(health({ status: "LIVE" }), now)).toBe("实时");
    expect(healthLabel(health({ status: "RECOVERING" }), now)).toBe("恢复中");
    expect(
      healthLabel(
        health({ status: "STALE", stale_since: now - 8_000, ts: now }),
        now,
      ),
    ).toBe("断流 8 秒");
    expect(healthLabel(null, now)).toBe("等待行情");
  });

  it("12 秒阈值判断用于提示文案", () => {
    const now = 1_763_596_812_000;
    const stale = health({
      status: "STALE",
      stale_since: now - STALE_AFTER_MS,
      ts: now,
    });
    expect(isStaleWindowExceeded(stale, now)).toBe(true);
    expect(
      isStaleWindowExceeded(
        health({ status: "STALE", stale_since: now - 1_000, ts: now }),
        now,
      ),
    ).toBe(false);
  });

  it("状态映射到徽标配色", () => {
    expect(healthTone("LIVE")).toBe("live");
    expect(healthTone("RECOVERING")).toBe("recovering");
    expect(healthTone("STALE")).toBe("stale");
  });
});
