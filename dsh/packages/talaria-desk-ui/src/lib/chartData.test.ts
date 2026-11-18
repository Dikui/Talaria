import { describe, expect, it } from "vitest";

import type { CompactBar } from "@talaria/contracts";

import { toKLineData, toKLineSeries, toPeriod } from "./chartData";

function bar(overrides: Partial<CompactBar> = {}): CompactBar {
  return {
    timestamp: 1_700_000_000_000,
    open: "101.10",
    high: "102.20",
    low: "100.00",
    close: "101.50",
    volume: "12.345",
    turnover: "1250.75",
    status: "NORMAL",
    ts_available: 1_700_000_060_000,
    precision: "SECOND_VERIFIED",
    ...overrides,
  };
}

describe("toKLineData", () => {
  it("只在图表边界把 DecimalString 转成 number，输入对象保持字符串", () => {
    const source = bar();
    const point = toKLineData(source);

    expect(point).not.toBeNull();
    expect(point?.open).toBe(101.1);
    expect(point?.volume).toBe(12.345);
    expect(source.open).toBe("101.10");
    expect(source.volume).toBe("12.345");
  });

  it("FEED_GAP 柱不进入 dataList", () => {
    const source = bar({
      status: "FEED_GAP",
      open: null,
      high: null,
      low: null,
      close: null,
      volume: null,
      turnover: null,
    });
    expect(toKLineData(source)).toBeNull();
  });
});

describe("toKLineSeries", () => {
  it("按时间升序排序且不修改输入数组", () => {
    const source = [
      bar({ timestamp: 3 }),
      bar({ timestamp: 1 }),
      bar({ timestamp: 2 }),
    ];
    const { points } = toKLineSeries(source);

    expect(points.map((point) => point.timestamp)).toEqual([1, 2, 3]);
    expect(source.map((item) => item.timestamp)).toEqual([3, 1, 2]);
  });

  it("统计断流数量并从 dataList 中过滤断流柱", () => {
    const gap = bar({
      timestamp: 2,
      status: "FEED_GAP",
      open: null,
      high: null,
      low: null,
      close: null,
      volume: null,
      turnover: null,
    });
    const { points, gapCount } = toKLineSeries([
      bar({ timestamp: 1 }),
      gap,
      bar({ timestamp: 3 }),
    ]);

    expect(points.map((point) => point.timestamp)).toEqual([1, 3]);
    expect(gapCount).toBe(1);
  });

  it("空数组返回空序列和零断流", () => {
    expect(toKLineSeries([])).toEqual({ points: [], gapCount: 0 });
  });
});

describe("toPeriod", () => {
  it("把周期标识映射为 KLineChart 的 period", () => {
    expect(toPeriod("1m")).toEqual({ type: "minute", span: 1 });
    expect(toPeriod("4h")).toEqual({ type: "hour", span: 4 });
    expect(toPeriod("1d")).toEqual({ type: "day", span: 1 });
  });
});
