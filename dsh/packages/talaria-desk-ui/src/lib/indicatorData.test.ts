import { describe, expect, it } from "vitest";
import type { IndicatorSeries } from "@talaria/contracts";

import {
  alignIndicatorRows,
  buildIndicatorValueMap,
  describeParams,
  toIndicatorRow,
} from "./indicatorData";

function series(overrides: Partial<IndicatorSeries> = {}): IndicatorSeries {
  return {
    name: "EMA",
    pane: "main",
    params: { period: 20, std_dev: null, anchor: null },
    points: [
      { timestamp: 1_000, values: { value: null } },
      { timestamp: 2_000, values: { value: "12.3400" } },
      { timestamp: 3_000, values: { value: null } },
      { timestamp: 4_000, values: { value: "13.5" } },
    ],
    summary: {
      latest: { value: "13.5" },
      warmup_bars: 1,
      valid_points: 2,
    },
    ...overrides,
  };
}

describe("indicatorData", () => {
  it("keeps DecimalString until the chart-data boundary", () => {
    const map = buildIndicatorValueMap(series());
    expect(map.get(2_000)).toEqual({ value: "12.3400" });
    expect(typeof map.get(2_000)?.value).toBe("string");
  });

  it("aligns rows to dataList order and length, using null for missing timestamps", () => {
    const dataList = [
      { timestamp: 2_000 },
      { timestamp: 3_000 },
      { timestamp: 5_000 },
      { timestamp: 4_000 },
    ];
    const rows = alignIndicatorRows(dataList, series());
    expect(rows).toHaveLength(dataList.length);
    expect(rows).toEqual([
      { value: 12.34 },
      { value: null },
      { value: null },
      { value: 13.5 },
    ]);
    expect(rows[0].value).toBeTypeOf("number");
  });

  it("does not turn null gaps into zero", () => {
    const map = buildIndicatorValueMap(series());
    expect(toIndicatorRow(3_000, map, ["value"])).toEqual({ value: null });
  });

  it("aligns BOLL three channels by timestamp", () => {
    const boll = series({
      name: "BOLL",
      params: { period: 20, std_dev: "2.5", anchor: null },
      points: [
        {
          timestamp: 2_000,
          values: { mid: "10", upper: "11", lower: "9" },
        },
      ],
      summary: { latest: { mid: "10", upper: "11", lower: "9" }, warmup_bars: 0, valid_points: 1 },
    });
    expect(alignIndicatorRows([{ timestamp: 2_000 }], boll)).toEqual([
      { mid: 10, upper: 11, lower: 9 },
    ]);
    expect(describeParams(boll)).toBe("BOLL(20, 2.5σ)");
  });

  it("describes VWAP anchor from backend params", () => {
    const vwap = series({
      name: "VWAP",
      params: { period: 1, std_dev: null, anchor: "session" },
      points: [{ timestamp: 1_000, values: { value: "100" } }],
      summary: { latest: { value: "100" }, warmup_bars: 0, valid_points: 1 },
    });
    expect(describeParams(vwap)).toBe("VWAP(UTC 日界)");
  });
});
