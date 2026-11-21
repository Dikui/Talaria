import { describe, expect, it } from "vitest";
import type { AlertRecord } from "@talaria/contracts";

import {
  ALERT_LEVEL_EVENTS,
  alertsForTab,
  buildAlertCondition,
  buildAlertCreate,
  compareAlerts,
  conditionSummary,
  defaultAlertForm,
  formatCooldown,
  relativeTime,
  statusToTab,
  tabCounts,
  validateAlertForm,
} from "./model";

function record(overrides: Partial<AlertRecord> = {}): AlertRecord {
  return {
    alert_id: "alr_1",
    instrument_id: "BTC-USDT-PERP",
    timeframe: "1m",
    condition: {
      kind: "METRIC_THRESHOLD",
      direction: "ABOVE",
      metric: "last_price",
      threshold: "100",
    },
    status: "ARMED",
    repeat: true,
    cooldown_ms: 60_000,
    note: "",
    created_ts: 1_000,
    updated_ts: 1_000,
    trigger_count: 0,
    ...overrides,
  };
}

describe("alerts relative time", () => {
  it("keeps sub-five-second gaps at 刚刚 and clamps future timestamps", () => {
    expect(relativeTime(1_000, 1_004)).toBe("刚刚");
    expect(relativeTime(1_000, 900)).toBe("刚刚");
  });

  it("renders seconds, minutes, hours and days", () => {
    expect(relativeTime(0, 12_000)).toBe("12 秒前");
    expect(relativeTime(0, 90_000)).toBe("1 分钟前");
    expect(relativeTime(0, 3 * 3_600_000)).toBe("3 小时前");
    expect(relativeTime(0, 2 * 86_400_000)).toBe("2 天前");
  });
});

describe("alert tab grouping", () => {
  it("maps every status onto one of the four list tabs", () => {
    expect(statusToTab("ARMED")).toBe("active");
    expect(statusToTab("COOLDOWN")).toBe("cooling");
    expect(statusToTab("DRAFT")).toBe("draft");
    expect(statusToTab("EXPIRED")).toBe("expired");
    expect(statusToTab("DISABLED")).toBe("expired");
  });

  it("filters a tab and orders the newest edit first", () => {
    const alerts = [
      record({ alert_id: "old", updated_ts: 10 }),
      record({ alert_id: "new", updated_ts: 30 }),
      record({ alert_id: "draft", status: "DRAFT", updated_ts: 99 }),
    ];
    expect(alertsForTab(alerts, "active").map((item) => item.alert_id)).toEqual([
      "new",
      "old",
    ]);
    expect(alertsForTab(alerts, "draft").map((item) => item.alert_id)).toEqual([
      "draft",
    ]);
    expect(tabCounts(alerts)).toEqual({
      active: 2,
      cooling: 0,
      expired: 0,
      draft: 1,
    });
  });

  it("uses the alert id as the deterministic tie breaker", () => {
    const left = record({ alert_id: "a", updated_ts: 5 });
    const right = record({ alert_id: "b", updated_ts: 5 });
    expect(compareAlerts(left, right)).toBeLessThan(0);
  });
});

describe("alert form validation", () => {
  it("rejects a metric form without a decimal threshold", () => {
    const form = defaultAlertForm("BTC-USDT-PERP", "1m");
    expect(validateAlertForm(form)).toBe("请输入有效的阈值");
    expect(validateAlertForm({ ...form, threshold: "101.5" })).toBeNull();
    expect(validateAlertForm({ ...form, threshold: "1e3" })).toBe("请输入有效的阈值");
  });

  it("requires a drawing for overlay alerts", () => {
    const form = { ...defaultAlertForm("BTC-USDT-PERP", "1m"), kind: "OVERLAY_CROSS" as const };
    expect(validateAlertForm(form)).toBe("请选择要挂载的画线");
    expect(validateAlertForm({ ...form, drawingId: "drw_1" })).toBeNull();
  });

  it("requires a threshold or a different compare indicator for indicator signals", () => {
    const form = {
      ...defaultAlertForm("BTC-USDT-PERP", "1m"),
      kind: "INDICATOR_SIGNAL" as const,
      indicator: "EMA" as const,
    };
    expect(validateAlertForm(form)).toBe("指标信号需要阈值或对比指标");
    expect(
      validateAlertForm({ ...form, compareIndicator: "EMA" }),
    ).toBe("对比指标不能与主指标相同");
    expect(
      validateAlertForm({ ...form, compareIndicator: "SMA", comparePeriod: 50 }),
    ).toBeNull();
  });

  it("rejects notes beyond 500 characters", () => {
    const form = { ...defaultAlertForm("BTC-USDT-PERP", "1m"), threshold: "1", note: "x".repeat(501) };
    expect(validateAlertForm(form)).toBe("备注最多 500 字");
  });
});

describe("alert payload building", () => {
  it("omits untouched periods so server defaults apply", () => {
    const form = {
      ...defaultAlertForm("BTC-USDT-PERP", "1m"),
      kind: "INDICATOR_SIGNAL" as const,
      indicator: "EMA" as const,
      threshold: "1",
    };
    expect(buildAlertCreate(form).condition).toEqual({
      kind: "INDICATOR_SIGNAL",
      direction: "ABOVE",
      indicator: "EMA",
      indicator_channel: "value",
      threshold: "1",
    });
  });

  it("sends VWAP anchors only for VWAP indicators", () => {
    const form = {
      ...defaultAlertForm("BTC-USDT-PERP", "1m"),
      kind: "INDICATOR_SIGNAL" as const,
      indicator: "VWAP" as const,
      indicatorAnchor: "snapshot" as const,
      compareIndicator: "VWAP" as const,
      compareAnchor: "session" as const,
      threshold: "",
    };
    expect(buildAlertCreate(form).condition).toEqual({
      kind: "INDICATOR_SIGNAL",
      direction: "ABOVE",
      indicator: "VWAP",
      indicator_channel: "value",
      indicator_anchor: "snapshot",
      compare_indicator: "VWAP",
      compare_anchor: "session",
    });
  });

  it("sends the threshold as a decimal string and converts cooldown to ms", () => {
    const form = {
      ...defaultAlertForm("BTC-USDT-PERP", "1m"),
      threshold: " 42000.5 ",
      cooldownSeconds: 90,
      note: "突破后观察",
    };
    expect(buildAlertCreate(form)).toMatchObject({
      instrument_id: "BTC-USDT-PERP",
      timeframe: "1m",
      draft: false,
      repeat: true,
      cooldown_ms: 90_000,
      condition: {
        kind: "METRIC_THRESHOLD",
        direction: "ABOVE",
        metric: "last_price",
        threshold: "42000.5",
      },
    });
  });

  it("turns a compare indicator into the crossing branch", () => {
    const form = {
      ...defaultAlertForm("BTC-USDT-PERP", "5m"),
      kind: "INDICATOR_SIGNAL" as const,
      direction: "BELOW" as const,
      indicator: "EMA" as const,
      indicatorPeriod: 20,
      compareIndicator: "SMA" as const,
      comparePeriod: 50,
      draft: true,
    };
    expect(buildAlertCreate(form).condition).toEqual({
      kind: "INDICATOR_SIGNAL",
      direction: "BELOW",
      indicator: "EMA",
      indicator_period: 20,
      indicator_channel: "value",
      compare_indicator: "SMA",
      compare_period: 50,
    });
  });
});

describe("alert summaries", () => {
  it("describes metric and overlay conditions with the latest drawing kind", () => {
    expect(
      conditionSummary({
        kind: "METRIC_THRESHOLD",
        direction: "BELOW",
        metric: "funding_rate",
        threshold: "-0.001",
      }),
    ).toBe("资金费率 ≤ -0.001");
    expect(
      conditionSummary(
        {
          kind: "OVERLAY_CROSS",
          direction: "ABOVE",
          drawing_id: "drw_1",
          overlay_mode: "TOUCH",
        },
        "HORIZONTAL_LINE",
      ),
    ).toBe("水平线 盘中触碰（上穿）");
  });

  it("formats cooldowns for the list", () => {
    expect(formatCooldown(0)).toBe("无冷却");
    expect(formatCooldown(120_000)).toBe("2 分钟");
    expect(formatCooldown(1_500)).toBe("1500 毫秒");
  });
});

describe("level event alerts", () => {
  it("defaults a key level seed to LEVEL_EVENT with TOUCH", () => {
    const form = defaultAlertForm("BTC-USDT-PERP", "1m", {
      levelId: "kl_0123456789abcdef",
      levelKind: "PREV_DAY_HIGH",
    });
    expect(form.kind).toBe("LEVEL_EVENT");
    expect(form.levelId).toBe("kl_0123456789abcdef");
    expect(form.levelEvent).toBe("TOUCH");
    expect(form.levelKind).toBe("PREV_DAY_HIGH");
    expect(validateAlertForm(form)).toBeNull();
  });

  it("offers TOUCH, BREAK and RETEST and builds only level fields", () => {
    expect(ALERT_LEVEL_EVENTS).toEqual(["TOUCH", "BREAK", "RETEST"]);
    const form = {
      ...defaultAlertForm("BTC-USDT-PERP", "1m", {
        levelId: "kl_0123456789abcdef",
        levelKind: "POC",
      }),
      levelEvent: "RETEST" as const,
    };
    expect(buildAlertCondition(form)).toEqual({
      kind: "LEVEL_EVENT",
      level_id: "kl_0123456789abcdef",
      level_event: "RETEST",
      level_kind: "POC",
    });
    expect(conditionSummary(buildAlertCondition(form))).toContain(
      "kl_0123456789abcdef",
    );
  });

  it("rejects a LEVEL_EVENT without a level id", () => {
    const form = defaultAlertForm("BTC-USDT-PERP", "1m");
    expect(validateAlertForm({ ...form, kind: "LEVEL_EVENT" })).toContain(
      "level_id",
    );
  });
});
