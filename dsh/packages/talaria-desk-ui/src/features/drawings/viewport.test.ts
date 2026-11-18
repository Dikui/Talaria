import { describe, expect, it } from "vitest";

import { priceWindow, timeWindow } from "./viewport";

const BARS = Array.from({ length: 10 }, (_, index) => ({
  timestamp: 1_000 + index * 60_000,
}));

describe("timeWindow", () => {
  it("把可视数据索引换成包含两端的时间窗口", () => {
    expect(timeWindow({ from: 2, to: 5 }, BARS)).toEqual({
      from_ts: 1_000 + 2 * 60_000,
      to_ts: 1_000 + 5 * 60_000,
    });
  });

  it("索引反向或越界时归一化到已加载范围", () => {
    expect(timeWindow({ from: 8, to: 3 }, BARS)).toEqual({
      from_ts: 1_000 + 3 * 60_000,
      to_ts: 1_000 + 8 * 60_000,
    });
    expect(timeWindow({ from: -5, to: 99 }, BARS)).toEqual({
      from_ts: 1_000,
      to_ts: 1_000 + 9 * 60_000,
    });
  });

  it("没有数据时返回 null", () => {
    expect(timeWindow({ from: 0, to: 3 }, [])).toBeNull();
  });
});

describe("priceWindow", () => {
  it("把主图像素上下沿换算成价格窗口，并固定 DecimalStr 精度", () => {
    // y 轴向下增大：上沿价格更高。
    expect(priceWindow(105.123, 99.876, 2)).toEqual({
      price_min: "99.88",
      price_max: "105.12",
    });
  });

  it("上下沿相同时给出退化的合法窗口", () => {
    expect(priceWindow(100, 100, 1)).toEqual({
      price_min: "100.0",
      price_max: "100.0",
    });
  });

  it("像素换算失败时返回 null，不写入 NaN", () => {
    expect(priceWindow(Number.NaN, 100, 2)).toBeNull();
    expect(priceWindow(100, Number.POSITIVE_INFINITY, 2)).toBeNull();
  });
});
