import { afterEach, describe, expect, it } from "vitest";
import type { AlertRecord, Rule } from "@talaria/contracts";

import { setLocale } from "../../lib/i18n";
import {
  ALERT_LEVEL_EVENTS,
  alertStatusLabel,
  alertTabLabel,
  alertsForTab,
  buildAlertRule,
  buildAlertCreate,
  compareAlerts,
  conditionSummary,
  defaultAlertForm,
  formatCooldown,
  formFromRecord,
  relativeTime,
  statusToTab,
  tabCounts,
  validateAlertForm,
} from "./model";

afterEach(() => setLocale("zh"));

function metricRule(value: string, metric: "last_price" | "funding_rate" = "last_price",
  test: "gte" | "lte" = "gte"): Rule {
  return {
    version: "1", timing: "event", expression: {
      op: "compare", test, left: { source: "event", metric },
      right: { source: "constant", value },
    },
  };
}

function closeRule(expression: Rule["expression"]): Rule {
  return { version: "1", timing: "bar_close", expression };
}

function record(overrides: Partial<AlertRecord> = {}): AlertRecord {
  return {
    alert_id: "alr_1",
    instrument_id: "BTC-USDT-PERP",
    timeframe: "1m",
    condition: metricRule("100"),
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
    const form = { ...defaultAlertForm("BTC-USDT-PERP", "1m"), kind: "overlay" as const };
    expect(validateAlertForm(form)).toBe("请选择要挂载的画线");
    expect(validateAlertForm({ ...form, drawingId: "drw_1" })).toBeNull();
  });

  it("requires a threshold or a different compare indicator for indicator signals", () => {
    const form = {
      ...defaultAlertForm("BTC-USDT-PERP", "1m"),
      kind: "indicator" as const,
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
      kind: "indicator" as const,
      indicator: "EMA" as const,
      threshold: "1",
    };
    expect(buildAlertCreate(form).condition).toEqual(closeRule({
      op: "compare", test: "gte",
      left: { source: "indicator", name: "EMA", channel: "value" },
      right: { source: "constant", value: "1" },
    }));
  });

  it("sends VWAP anchors only for VWAP indicators", () => {
    const form = {
      ...defaultAlertForm("BTC-USDT-PERP", "1m"),
      kind: "indicator" as const,
      indicator: "VWAP" as const,
      indicatorAnchor: "snapshot" as const,
      compareIndicator: "VWAP" as const,
      compareAnchor: "session" as const,
      threshold: "",
    };
    expect(buildAlertCreate(form).condition).toEqual(closeRule({
      op: "compare", test: "gte",
      left: { source: "indicator", name: "VWAP", channel: "value", params: { anchor: "snapshot" } },
      right: { source: "indicator", name: "VWAP", params: { anchor: "session" } },
    }));
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
      condition: metricRule("42000.5"),
    });
  });

  it("turns a compare indicator into the crossing branch", () => {
    const form = {
      ...defaultAlertForm("BTC-USDT-PERP", "5m"),
      kind: "indicator" as const,
      direction: "BELOW" as const,
      indicator: "EMA" as const,
      indicatorPeriod: 20,
      compareIndicator: "SMA" as const,
      comparePeriod: 50,
      draft: true,
    };
    expect(buildAlertCreate(form).condition).toEqual(closeRule({
      op: "compare", test: "lte",
      left: { source: "indicator", name: "EMA", channel: "value", params: { period: 20 } },
      right: { source: "indicator", name: "SMA", params: { period: 50 } },
    }));
  });
});

describe("alert summaries", () => {
  it("describes metric and overlay conditions with the latest drawing kind", () => {
    expect(
      conditionSummary(metricRule("-0.001", "funding_rate", "lte")),
    ).toBe("资金费率 ≤ -0.001");
    expect(
      conditionSummary(
        {
          version: "1", timing: "event", expression: {
            op: "overlay", direction: "ABOVE", drawing_id: "drw_1", mode: "TOUCH",
          },
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
  it("defaults a key level seed to level with TOUCH", () => {
    const form = defaultAlertForm("BTC-USDT-PERP", "1m", {
      levelId: "kl_0123456789abcdef",
      levelKind: "PREV_DAY_HIGH",
    });
    expect(form.kind).toBe("level");
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
    expect(buildAlertRule(form)).toEqual(closeRule({
      op: "level_event", level_id: "kl_0123456789abcdef",
      event: "RETEST", level_kind: "POC",
    }));
    expect(conditionSummary(buildAlertRule(form))).toContain(
      "kl_0123456789abcdef",
    );
  });

  it("rejects a level without a level id", () => {
    const form = defaultAlertForm("BTC-USDT-PERP", "1m");
    expect(validateAlertForm({ ...form, kind: "level" })).toContain(
      "level_id",
    );
  });
});

it("lets the server supply both untouched VWAP anchors", () => {
  const form = {
    ...defaultAlertForm("BTC-USDT-PERP", "1m"),
    kind: "indicator" as const,
    indicator: "VWAP" as const,
    compareIndicator: "VWAP" as const,
  };
  const condition = buildAlertCreate(form).condition;
  expect(condition).not.toHaveProperty("expression.left.params.anchor");
  expect(condition).not.toHaveProperty("expression.right.params.anchor");
});


it("preserves an existing composite rule when saving alert settings", () => {
  const condition: Rule = {
    version: "1",
    timing: "bar_close",
    expression: {
      op: "all",
      args: [{
        op: "compare",
        test: "gt",
        left: { source: "bar", field: "close" },
        right: { source: "constant", value: "100" },
      }],
    },
  };
  const form = formFromRecord(record({ condition }));
  expect(buildAlertCreate({ ...form, note: "新备注" }).condition).toEqual(condition);
});


it("re-localizes labels at render time when the locale switches", () => {
  expect(alertTabLabel("cooling")).toBe("冷却");
  expect(alertStatusLabel("ARMED")).toBe("待命");
  expect(relativeTime(1_000, 61_000)).toBe("1 分钟前");

  setLocale("en-US");
  expect(alertTabLabel("cooling")).toBe("Cooling");
  expect(alertStatusLabel("ARMED")).toBe("Armed");
  expect(relativeTime(1_000, 61_000)).toBe("1m ago");
  expect(formatCooldown(0)).toBe("No cooldown");
  expect(conditionSummary(metricRule("100"))).toContain("Last price");
  expect(conditionSummary({
    version: "1", timing: "event", expression: {
      op: "overlay", direction: "ABOVE", drawing_id: "drw_1", mode: "TOUCH",
    },
  }, "HORIZONTAL_LINE")).toBe("Horizontal line intraday touch (crosses above)");

  setLocale("zh-CN");
  expect(alertStatusLabel("ARMED")).toBe("待命");
  expect(relativeTime(1_000, 61_000)).toBe("1 分钟前");
});
