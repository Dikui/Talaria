import { afterEach, describe, expect, it } from "vitest";
import type { ScannerTrigger } from "@talaria/contracts";

import { setLocale } from "../../lib/i18n";
import {
  evidenceLabel,
  focusLocatePayload,
  locatePayload,
  mergeTrigger,
  summaryNeedsCollapse,
  summaryPreview,
  scannerKindLabel,
  scannerSeverityLabel,
  triggerTags,
  triggerTime,
} from "./model";

afterEach(() => setLocale("zh"));

export function trigger(overrides: Partial<ScannerTrigger> = {}): ScannerTrigger {
  return {
    trigger_id: "scn_1",
    instrument_id: "BTC-USDT-PERP",
    timeframe: "1m",
    kind: "LIQUIDITY_ANOMALY",
    severity: "WARNING",
    title: "流动性异常",
    summary: "成交额较近 60 根均值放大 3.2σ（分位 99%，倍数 3.10×）",
    evidence: [
      { metric: "zscore", value: "3.2" },
      { metric: "quantile", value: "0.99" },
    ],
    tags: ["流动性", "1m", "z=3.2"],
    created_ts: 1_000_000,
    snapshot_id: "snap_1",
    route: "LOCAL",
    ...overrides,
  };
}

describe("scanner card model", () => {
  it("builds stable kind-first tags and readable evidence", () => {
    expect(triggerTags(trigger())).toEqual([
      "流动性异常",
      "流动性",
      "1m",
      "z=3.2",
    ]);
    expect(evidenceLabel({ metric: "quantile", value: "0.990" })).toBe(
      "分位 99%",
    );
    expect(evidenceLabel({ metric: "distance", value: "0.000427" })).toBe(
      "距离 0.04%",
    );
  });

  it("formats trigger time relative to now", () => {
    expect(triggerTime(1_000, 1_004)).toBe("刚刚");
    expect(triggerTime(1_000, 61_000)).toBe("1 分钟前");
  });

  it("collapses long multi-indicator summaries but keeps short ones open", () => {
    expect(summaryNeedsCollapse(trigger())).toBe(false);
    const long = trigger({
      summary:
        "成交量 4.8σ、真实波幅分位 99%、资金费率 99 分位、OI 与价格反向，且价格触及高强度前高 112345.6；多个指标同时异常，需要展开查看完整解释。",
      tags: ["流动性", "波动率", "资金费率", "OI", "关键位"],
    });
    expect(summaryNeedsCollapse(long)).toBe(true);
    expect(summaryPreview(long.summary).endsWith("…")).toBe(true);
  });

  it("payload for a card click switches instrument, timeframe and snapshot cutoff", () => {
    expect(locatePayload(trigger())).toEqual({
      instrument_id: "BTC-USDT-PERP",
      timeframe: "1m",
      triggered_ts: 1_000_000,
    });
  });

  it("focus chip locates with the last trigger timeframe", () => {
    expect(
      focusLocatePayload({
        instrument_id: "ETH-USDT-PERP",
        last_trigger_ts: 2_000,
        trigger_count: 3,
        severity: "CRITICAL",
        last_timeframe: "5m",
      }),
    ).toEqual({
      instrument_id: "ETH-USDT-PERP",
      timeframe: "5m",
      triggered_ts: 2_000,
    });
  });

  it("dedupes WS and polling copies by trigger_id", () => {
    const original = trigger();
    expect(mergeTrigger([original], original)).toHaveLength(1);
    expect(mergeTrigger([], original)).toHaveLength(1);
  });
});


it("formats kind, severity, evidence and relative time in the active locale", () => {
  setLocale("en-US");
  expect(scannerKindLabel("LIQUIDITY_ANOMALY")).toBe("Liquidity anomaly");
  expect(scannerSeverityLabel("CRITICAL")).toBe("Strong move");
  expect(triggerTags(trigger())[0]).toBe("Liquidity anomaly");
  expect(evidenceLabel({ metric: "quantile", value: "0.990" })).toBe("quantile 99%");
  expect(triggerTime(1_000, 61_000)).toBe("1m ago");
  expect(triggerTime(0, 2 * 86_400_000)).toMatch(/^\d{2}\/\d{2}, \d{2}:\d{2}$/);

  setLocale("zh-CN");
  expect(triggerTags(trigger())[0]).toBe("流动性异常");
  expect(triggerTime(1_000, 61_000)).toBe("1 分钟前");
  expect(triggerTime(0, 2 * 86_400_000)).toMatch(/^\d{2}\/\d{2} \d{2}:\d{2}$/);
});
