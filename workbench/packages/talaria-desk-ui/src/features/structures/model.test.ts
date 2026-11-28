import { describe, expect, it } from "vitest";
import type { StructureFeature } from "@talaria/contracts";

import { getLocale, setLocale } from "../../lib/i18n";
import {
  catalogDetectors,
  defaultStructureFilter,
  detectorLabel,
  disableAllDetectors,
  enableAllDetectors,
  formatStructureGeometry,
  higherTimeframesFor,
  toggleDetectorId,
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

  it("localizes built-in detector IDs in zh/en, shows non-v1 versions, and falls back for unknown IDs", () => {
    const prev = getLocale();
    try {
      const cases: Array<[id: string, zh: string, en: string]> = [
        ["confluence@1", "多维共振带", "Confluence zones"],
        ["convex_envelope@1", "凸包包络线", "Convex envelope"],
        ["equal_levels@1", "等高/等低位", "Equal levels"],
        ["fvg@1", "FVG 缺口", "Fair value gap"],
        ["gap_levels@1", "跳空缺口", "Price gaps"],
        ["liquidation_zones@1", "清算估算区", "Liquidation zones"],
        ["pivot_trendline@1", "摆动趋势线", "Pivot trendline"],
        ["round_numbers@1", "整数关口", "Round numbers"],
        ["session_levels@1", "时段关键位", "Session levels"],
        ["swing_levels@1", "摆动高低位", "Swing levels"],
        ["volume_profile@1", "量价/VWAP", "Volume profile"],
      ];

      setLocale("zh");
      for (const [id, zh] of cases) {
        expect(detectorLabel(id)).toBe(zh);
      }
      expect(detectorLabel("pivot_trendline@2")).toBe("摆动趋势线 v2");
      expect(detectorLabel("custom_algo@1")).toBe("custom_algo@1");

      setLocale("en");
      for (const [id, , en] of cases) {
        expect(detectorLabel(id)).toBe(en);
      }
      expect(detectorLabel("pivot_trendline@2")).toBe("Pivot trendline v2");
      expect(detectorLabel("custom_algo@1")).toBe("custom_algo@1");
    } finally {
      setLocale(prev);
    }
  });

  it("unplugs and re-plugs single detectors across simple and expert modes", () => {
    const simpleBase: StructureFilter = { ...defaultStructureFilter, enabled: true, viewMode: "simple" };
    const unplugged = toggleDetectorId(simpleBase, "confluence@1");
    expect(unplugged.disabledDetectorIds).toEqual(["confluence@1"]);
    expect(visibleStructures(all, unplugged, new Set()).map((s) => s.structure_id)).toEqual([
      "st_trend_1",
    ]);

    const expertUnplugged: StructureFilter = { ...unplugged, viewMode: "expert" };
    expect(visibleStructures(all, expertUnplugged, new Set()).map((s) => s.structure_id)).toEqual([
      "st_trend_1",
      "st_lvl_1",
      "st_chan_1",
    ]);

    const replugged = toggleDetectorId(unplugged, "confluence@1");
    expect(replugged.disabledDetectorIds).toEqual([]);
    expect(visibleStructures(all, replugged, new Set()).map((s) => s.structure_id)).toEqual([
      "st_conf_1",
      "st_trend_1",
    ]);

    const fullCatalog = catalogDetectors([], all);
    const allDisabled = disableAllDetectors(replugged, fullCatalog);
    expect(allDisabled.disabledDetectorIds).toEqual(fullCatalog);
    expect(visibleStructures(all, allDisabled, new Set())).toEqual([]);
    expect(enableAllDetectors(allDisabled).disabledDetectorIds).toEqual([]);
  });

  it("builds version-sorted catalog including 0-count registered detectors and disambiguates multi-version labels", () => {
    const prev = getLocale();
    try {
      const catalog = catalogDetectors(
        ["gap_levels@1", "pivot_trendline@2", "pivot_trendline@1"],
        [trend, level],
      );
      expect(catalog).toEqual([
        "confluence@1",
        "gap_levels@1",
        "pivot_trendline@1",
        "pivot_trendline@2",
        "swing@1",
      ]);

      setLocale("zh");
      expect(detectorLabel("pivot_trendline@1", catalog)).toBe("摆动趋势线 v1");
      expect(detectorLabel("pivot_trendline@2", catalog)).toBe("摆动趋势线 v2");
      expect(detectorLabel("gap_levels@1", catalog)).toBe("跳空缺口");

      setLocale("en");
      expect(detectorLabel("pivot_trendline@1", catalog)).toBe("Pivot trendline v1");
      expect(detectorLabel("pivot_trendline@2", catalog)).toBe("Pivot trendline v2");
    } finally {
      setLocale(prev);
    }
  });
});
