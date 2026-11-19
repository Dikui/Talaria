import { describe, expect, it } from "vitest";
import type { ScannerBriefing } from "@talaria/contracts";

import {
  abstainReasonLabel,
  briefingLocate,
  briefingNeedsCollapse,
  briefingPreview,
  briefingQuestion,
  briefingStatusLabel,
  briefingTime,
} from "./model";

const NOW = 1_795_023_000_000;

function briefing(overrides: Partial<ScannerBriefing> = {}): ScannerBriefing {
  return {
    id: "brf_1",
    trigger_id: "scn_1",
    snapshot_id: "snap_1",
    instrument_id: "ETH-USDT-PERP",
    timeframe: "5m",
    status: "READY",
    summary: {
      trigger_reason: "放量突破",
      key_levels: [],
      risks: [],
    },
    evidence_refs: [],
    abstain_reason: null,
    created_ts: NOW - 120_000,
    read_ts: null,
    ...overrides,
  };
}

describe("briefing model", () => {
  it("maps status and abstain reasons to readable Chinese", () => {
    expect(briefingStatusLabel("READY")).toBe("已就绪");
    expect(abstainReasonLabel("MODEL_TIMEOUT")).toContain("超时");
    expect(abstainReasonLabel("HOST_OFFLINE")).toContain("--solo");
    expect(abstainReasonLabel("UNKNOWN_REASON")).toContain("UNKNOWN_REASON");
  });

  it("collapses only long trigger reasons", () => {
    expect(briefingNeedsCollapse(briefing())).toBe(false);
    expect(briefingNeedsCollapse(briefing({ summary: {
      trigger_reason: "长".repeat(121),
      key_levels: [],
      risks: [],
    } }))).toBe(true);
    expect(briefingPreview("短摘要")).toBe("短摘要");
    expect(briefingPreview("长".repeat(130)).endsWith("…")).toBe(true);
  });

  it("builds the chart jump payload from the trigger snapshot time", () => {
    expect(briefingLocate(briefing())).toEqual({
      instrument_id: "ETH-USDT-PERP",
      timeframe: "5m",
      triggered_ts: NOW - 120_000,
    });
  });

  it("builds a consultation question bound to the snapshot", () => {
    const question = briefingQuestion(briefing());
    expect(question).toContain("ETH-USDT-PERP");
    expect(question).toContain("5m");
    expect(question).toContain("snap_1");
    expect(question).toContain("放量突破");
  });

  it("formats relative time", () => {
    expect(briefingTime(NOW - 30_000, NOW)).toBe("刚刚");
    expect(briefingTime(NOW - 120_000, NOW)).toBe("2 分钟前");
    expect(briefingTime(0, NOW)).toBe("时间未知");
  });
});
