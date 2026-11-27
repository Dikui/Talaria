import { describe, expect, it } from "vitest";
import type { StructureFeature } from "@talaria/contracts";

import {
  defaultStructureFilter,
  formatStructureGeometry,
  higherTimeframesFor,
  toggleFamily,
  visibleStructures,
  type StructureFilter,
} from "./model";

function makeStructure(partial: Partial<StructureFeature>): StructureFeature {
  return {
    structure_id: "st_1",
    instrument_id: "BTC-USDT-PERP",
    detector_id: "pivot_trendline@1",
    family: "TRENDLINE",
    category: "TREND",
    kind: "TRENDLINE_SUPPORT",
    evidence_class: "OBSERVED",
    timeframe: "1m",
    source_timeframe: "1m",
    geometry: {
      kind: "SLOPE_LINE",
      anchor_start: { ts: 1_700_000_000_000, price: "68000" },
      anchor_end: { ts: 1_700_000_600_000, price: "68500" },
      scale: "LINEAR",
      extend_right: true,
    },
    strength: 0.85,
    status: "ACTIVE",
    ts_available: 1_700_000_600_000,
    formed_ts: 1_700_000_000_000,
    analysis_window: { from_ts: 1_700_000_000_000, to_ts: 1_700_000_600_000 },
    ...partial,
  };
}

describe("Market structure model & Simple/Expert filtering", () => {
  const confluence = makeStructure({
    structure_id: "st_conf_1",
    detector_id: "confluence@1",
    family: "CONFLUENCE",
    category: "CONFLUENCE",
    kind: "CONFLUENCE_ZONE",
    geometry: { kind: "HORIZONTAL_ZONE", price_low: "68100", price_high: "68250" },
    strength: 0.9,
  });
  const trend = makeStructure({ structure_id: "st_trend_1", strength: 0.8 });
  const level = makeStructure({
    structure_id: "st_lvl_1",
    detector_id: "swing@1",
    family: "LEVEL",
    category: "STRUCTURE",
    kind: "SWING_HIGH",
    geometry: { kind: "HORIZONTAL_LINE", price: "69000" },
    strength: 0.75,
  });
  const channel = makeStructure({
    structure_id: "st_chan_1",
    detector_id: "channel@1",
    family: "CHANNEL",
    category: "TREND",
    kind: "CHANNEL_UP",
    geometry: {
      kind: "CHANNEL",
      upper: {
        kind: "SLOPE_LINE",
        anchor_start: { ts: 1_700_000_000_000, price: "68500" },
        anchor_end: { ts: 1_700_000_600_000, price: "69000" },
        scale: "LINEAR",
        extend_right: true,
      },
      lower: {
        kind: "SLOPE_LINE",
        anchor_start: { ts: 1_700_000_000_000, price: "68000" },
        anchor_end: { ts: 1_700_000_600_000, price: "68500" },
        scale: "LINEAR",
        extend_right: true,
      },
    },
    strength: 0.7,
  });

  const all = [confluence, trend, level, channel];

  it("shows CONFLUENCE and top TRENDLINE in simple mode", () => {
    const filter: StructureFilter = { ...defaultStructureFilter, enabled: true, viewMode: "simple" };
    const result = visibleStructures(all, filter, new Set(["st_conf_none"]));
    expect(result.map((s) => s.structure_id)).toEqual(["st_conf_1", "st_trend_1"]);
  });

  it("filters by family, detector, and ignoredIds in expert mode", () => {
    let filter: StructureFilter = { ...defaultStructureFilter, enabled: true, viewMode: "expert" };
    filter = toggleFamily(filter, "TRENDLINE"); // hide trendlines
    const result = visibleStructures(all, filter, new Set(["st_lvl_1"]));
    expect(result.map((s) => s.structure_id)).toEqual(["st_conf_1", "st_chan_1"]);
  });

  it("formats all 4 geometry primitives and computes HTF candidates", () => {
    expect(formatStructureGeometry(level, 1)).toBe("69000.0");
    expect(formatStructureGeometry(confluence, 0)).toBe("68100–68250");
    expect(formatStructureGeometry(trend, 0)).toBe("68000 → 68500");
    expect(formatStructureGeometry(channel, 0)).toBe("68500–69000");
    expect(higherTimeframesFor("15m")).toEqual(["1h", "4h", "1d"]);
  });
});
