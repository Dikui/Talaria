import { describe, expect, it } from "vitest";
import type {
  Abstain,
  ConsultationRecord,
  EvidenceRef,
  UsageSummary,
} from "@talaria/contracts";

import {
  abstainCopy,
  consultationStatusLabel,
  directionLabel,
  evidenceCount,
  evidenceLabel,
  groupEvidence,
  isPending,
  reasonTitle,
  usageLine,
} from "./model";

function evidence(overrides: Partial<EvidenceRef> = {}): EvidenceRef {
  return {
    evidence_id: "ev_1",
    kind: "KEY_LEVEL",
    label: "前高",
    detail: "",
    price_low: null,
    price_high: null,
    ...overrides,
  };
}

function abstain(overrides: Partial<Abstain> = {}): Abstain {
  return {
    reason_code: "STALE_SNAPSHOT",
    message: "快照冻结超过 15 分钟。",
    evidence_refs: [],
    ...overrides,
  };
}

function record(overrides: Partial<ConsultationRecord> = {}): ConsultationRecord {
  return {
    consultation_id: "cons_1",
    snapshot_id: "snap_1",
    instrument_id: "BTC-USDT-PERP",
    timeframe: "1m",
    status: "PENDING",
    package: {
      consultation_id: "cons_1",
      snapshot_id: "snap_1",
      instrument_id: "BTC-USDT-PERP",
      timeframe: "1m",
      visible: {
        from_ts: 1,
        to_ts: 2,
        price_min: "100",
        price_max: "110",
      },
      selection: null,
      drawings: [],
      catalysts: [],
      question: "现在适合入场吗？",
      snapshot_cutoff_ts: 1,
      evidence_catalog: [],
      created_ts: 1,
    },
    insight: null,
    abstain: null,
    error: null,
    dsh_session_id: null,
    dsh_turn_id: null,
    created_ts: 1,
    updated_ts: 1,
    completed_ts: null,
    ...overrides,
  };
}

describe("abstain presentation", () => {
  it("labels all four required abstain reasons", () => {
    expect(reasonTitle("STALE_SNAPSHOT")).toBe("快照已过期");
    expect(reasonTitle("EVIDENCE_CONFLICT")).toBe("关键证据冲突");
    expect(reasonTitle("GEOMETRY_INVERTED")).toBe("几何关系倒挂");
    expect(reasonTitle("MODEL_TIMEOUT")).toBe("推理超时");
  });

  it("keeps host and invalid-output fallbacks readable", () => {
    expect(abstainCopy(abstain({ reason_code: "HOST_OFFLINE" })).title).toBe(
      "协同宿主离线",
    );
    expect(
      abstainCopy(abstain({ reason_code: "MODEL_INVALID_OUTPUT" })).title,
    ).toBe("输出无法核对");
  });

  it("never invents a conclusion for an unknown reason", () => {
    const copy = abstainCopy({
      ...abstain(),
      reason_code: "FUTURE_REASON" as Abstain["reason_code"],
    });
    expect(copy.title).toBe("本次会诊已放弃");
    expect(copy.hint).toContain("快照冻结超过");
  });
});

describe("evidence folding", () => {
  it("groups by kind in first-seen order and hides duplicates", () => {
    const groups = groupEvidence([
      evidence({ evidence_id: "ev_1", kind: "KEY_LEVEL" }),
      evidence({ evidence_id: "ev_2", kind: "SNAPSHOT" }),
      evidence({ evidence_id: "ev_1", kind: "KEY_LEVEL" }),
      evidence({ evidence_id: "ev_3", kind: "HUMAN_DRAWING" }),
    ]);
    expect(groups.map((group) => group.kind)).toEqual([
      "KEY_LEVEL",
      "SNAPSHOT",
      "HUMAN_DRAWING",
    ]);
    expect(groups[0].label).toBe("关键位");
    expect(groups[0].items).toHaveLength(1);
  });

  it("shows a price range only when evidence carries one", () => {
    expect(evidenceLabel(evidence())).toBe("前高");
    expect(
      evidenceLabel(
        evidence({ price_low: "60000", price_high: "60100.5" }),
      ),
    ).toBe("前高 · 60000–60100.5");
  });

  it("counts unique evidence refs", () => {
    expect(
      evidenceCount([
        evidence({ evidence_id: "a" }),
        evidence({ evidence_id: "a" }),
        evidence({ evidence_id: "b" }),
      ]),
    ).toBe(2);
    expect(evidenceCount(undefined)).toBe(0);
  });
});

describe("consultation state and usage", () => {
  it("maps statuses, directions and pending state", () => {
    expect(consultationStatusLabel(record())).toBe("等待协同宿主");
    expect(consultationStatusLabel(record({ status: "READY" }))).toBe(
      "Setup Insight",
    );
    expect(consultationStatusLabel(record({ status: "ABSTAIN" }))).toBe(
      "已放弃（Abstain）",
    );
    expect(consultationStatusLabel(record({ status: "FAILED" }))).toBe(
      "会诊失败",
    );
    expect(directionLabel("LONG")).toBe("偏多观察");
    expect(directionLabel("SHORT")).toBe("偏空观察");
    expect(directionLabel("NEUTRAL")).toBe("中性观察");
    expect(isPending(record())).toBe(true);
    expect(isPending(record({ status: "READY" }))).toBe(false);
  });

  it("marks missing usage instead of pretending zero tokens", () => {
    const summary: UsageSummary = {
      dsh_session_id: null,
      input_tokens: 120,
      cached_input_tokens: 40,
      output_tokens: 30,
      turns: 1,
      missing_reports: 2,
    };
    expect(usageLine(summary)).toBe(
      "输入 120 · 缓存命中 40 · 输出 30 · 1 轮 · 未报 2 轮",
    );
    expect(usageLine(null)).toBe("Token 用量暂不可用");
  });
});
