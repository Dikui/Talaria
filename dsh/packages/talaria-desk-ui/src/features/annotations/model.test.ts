import { afterEach, describe, expect, it } from "vitest";

import { setLocale } from "../../lib/i18n";
import type { AgentAnnotationRecord } from "@talaria/contracts";

import { isReadOnlyViewport } from "../drawings/responsive";
import {
  activeAnnotations,
  annotationPopoverLines,
  annotationPriceLabel,
  annotationStatusLabel,
  kindLabel,
  annotationZoneEnd,
  canManageAnnotations,
  evidenceLines,
  filterAnnotations,
  summarizeSessions,
} from "./model";

function annotation(
  overrides: Partial<AgentAnnotationRecord> = {},
): AgentAnnotationRecord {
  return {
    annotation_id: "ann_1",
    snapshot_id: "snap_abc",
    instrument_id: "BTC-USDT-PERP",
    timeframe: "1m",
    session_id: "session-a",
    round: 2,
    kind: "ZONE",
    status: "ACTIVE",
    label: "回踩承接",
    rationale: "前低与成交量密集区重叠",
    price_low: "99000.0",
    price_high: "99500.0",
    ts_anchor: 1_700_000_000_000,
    evidence_refs: [
      { level_id: "kl_1", note: "前低" },
      { kline_ts: 1_700_000_060_000 },
    ],
    created_ts: 1_700_000_100_000,
    updated_ts: 1_700_000_100_000,
    ...overrides,
  };
}

describe("annotation model", () => {
  it("只在图层绘制 ACTIVE 建议", () => {
    const records = [
      annotation(),
      annotation({ annotation_id: "ann_2", status: "ADOPTED" }),
      annotation({ annotation_id: "ann_3", status: "DISMISSED" }),
      annotation({ annotation_id: "ann_4", status: "ARCHIVED" }),
    ];
    expect(activeAnnotations(records).map((item) => item.annotation_id)).toEqual([
      "ann_1",
    ]);
  });

  it("按会话过滤，空会话返回全部", () => {
    const records = [
      annotation(),
      annotation({ annotation_id: "ann_2", session_id: "session-b" }),
    ];
    expect(filterAnnotations(records, "session-b")).toHaveLength(1);
    expect(filterAnnotations(records, null)).toHaveLength(2);
  });

  it("会话汇总忽略归档并按最新时间排序", () => {
    const sessions = summarizeSessions([
      annotation({ session_id: "session-a", updated_ts: 10 }),
      annotation({ annotation_id: "ann_2", session_id: "session-a", updated_ts: 20 }),
      annotation({ annotation_id: "ann_3", session_id: "session-b", updated_ts: 30 }),
      annotation({
        annotation_id: "ann_4",
        session_id: "session-b",
        status: "ARCHIVED",
        updated_ts: 40,
      }),
    ]);
    expect(sessions.map((item) => item.session_id)).toEqual([
      "session-b",
      "session-a",
    ]);
    expect(sessions[1].annotation_count).toBe(2);
  });

  it("证据同时支持关键位与具体 K 线时间戳", () => {
    expect(evidenceLines(annotation())).toEqual([
      "关键位 kl_1（前低）",
      expect.stringContaining("K 线"),
    ]);
    expect(annotationPopoverLines(annotation())).toEqual([
      "快照 snap_abc",
      "第 2 轮 · session-a",
      "证据：关键位 kl_1（前低）",
      expect.stringContaining("证据：K 线"),
    ]);
  });

  it("LEVEL 显示单一价格，ZONE 显示价格带", () => {
    expect(annotationPriceLabel(annotation())).toBe("99000.0–99500.0");
    expect(
      annotationPriceLabel(
        annotation({ kind: "LEVEL", price_low: "99000.0", price_high: "99000.0" }),
      ),
    ).toBe("99000.0");
  });

  it("价格带按 100 根当前周期向前延伸", () => {
    expect(annotationZoneEnd(annotation(), 60_000)).toBe(1_700_006_000_000);
  });

  it("390 只读隐藏操作，1440 可操作", () => {
    expect(isReadOnlyViewport(390)).toBe(true);
    expect(canManageAnnotations(isReadOnlyViewport(390))).toBe(false);
    expect(canManageAnnotations(isReadOnlyViewport(1440))).toBe(true);
  });
});

describe("annotation model (English)", () => {
  afterEach(() => setLocale("zh"));

  it("localizes status, kind, evidence and popover lines", () => {
    setLocale("en");
    expect(annotationStatusLabel("ACTIVE")).toBe("Active");
    expect(kindLabel("ZONE")).toBe("Price zone");
    expect(annotationPopoverLines(annotation())).toEqual([
      "Snapshot snap_abc",
      "Round 2 · session-a",
      "Evidence: Key level kl_1 (前低)",
      expect.stringMatching(/^Evidence: Candle /),
    ]);
  });
});
