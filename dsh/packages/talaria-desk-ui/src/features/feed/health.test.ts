import { afterEach, describe, expect, it } from "vitest";

import { setLocale } from "../../lib/i18n";

import {
  feedAlertView,
  feedBadgeView,
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
    timeframe: "1m",
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

  it("浏览器连接断开时徽标不再显示旧的 LIVE health", () => {
    const now = 1_763_596_808_000;
    const live = health({ status: "LIVE", ts: now });
    expect(feedBadgeView(live, now, true, "open")).toEqual({
      status: "LIVE",
      label: "实时",
      tone: "live",
    });
    expect(feedBadgeView(live, now, true, "closed")).toEqual({
      status: "DISCONNECTED",
      label: "连接断开 · 重连中",
      tone: "disconnected",
    });
    expect(feedBadgeView(live, now, true, "connecting")).toEqual({
      status: "DISCONNECTED",
      label: "连接断开 · 重连中",
      tone: "disconnected",
    });
    // 首次连接尚未收到 hello：显示等待行情，不误报“连接断开”。
    expect(feedBadgeView(null, now, null, "connecting")).toEqual({
      status: "WAITING",
      label: "等待行情",
      tone: "waiting",
    });
  });

  it("离线样本模式不会伪装成等待行情", () => {
    const now = 1_763_596_808_000;
    expect(feedBadgeView(null, now, false, "open")).toEqual({
      status: "OFFLINE",
      label: "无实时流",
      tone: "offline",
    });
    expect(feedBadgeView(null, now, true, "open")).toEqual({
      status: "WAITING",
      label: "等待行情",
      tone: "waiting",
    });
  });

  it("提示条优先提示本地连接断开，其次才是行情断流", () => {
    const now = 1_763_596_808_000;
    const stale = health({
      status: "STALE",
      stale_since: now - 8_000,
      ts: now,
    });
    expect(feedAlertView(stale, now, true, "open")).toEqual({
      title: "实时行情断流",
      detail:
        "已持续 8 秒，正在等待重连与回填；断流期间的 K 线会以 FEED_GAP 留空，不参与指标计算。",
    });
    expect(feedAlertView(stale, now, true, "closed")?.title).toBe("连接已断开");
    // 首次连接就直接失败（没有 hello）也要提示连接断开。
    expect(feedAlertView(null, now, null, "closed")?.title).toBe("连接已断开");
    expect(feedAlertView(health({ status: "LIVE" }), now, true, "open")).toBeNull();
    expect(feedAlertView(null, now, false, "open")).toBeNull();
  });

  it("状态映射到徽标配色", () => {
    expect(healthTone("LIVE")).toBe("live");
    expect(healthTone("RECOVERING")).toBe("recovering");
    expect(healthTone("STALE")).toBe("stale");
  });
});

describe("feed health (English)", () => {
  afterEach(() => setLocale("zh"));

  it("formats durations and labels in English", () => {
    setLocale("en");
    const now = 1_763_596_808_000;
    expect(formatDuration(8_400)).toBe("8s");
    expect(formatDuration(125_000)).toBe("2m 5s");
    expect(healthLabel(health({ status: "LIVE" }), now)).toBe("Live");
    expect(healthLabel(health({ status: "RECOVERING" }), now)).toBe("Recovering");
    expect(
      healthLabel(
        health({ status: "STALE", stale_since: now - 8_000, ts: now }),
        now,
      ),
    ).toBe("Stream stale 8s");
    expect(healthLabel(null, now)).toBe("Waiting for market data");
    expect(feedBadgeView(null, now, false, "open")).toEqual({
      status: "OFFLINE",
      label: "No live stream",
      tone: "offline",
    });
  });
});
