import { expect, it, vi } from "vitest";
import type { Chart } from "klinecharts";
import { applyLinkedCrosshair, linkedCrosshairTime } from "../../src/features/market-chart/crosshair";

// 平台 02：接收端用自己的坐标驱动 KLineChart 十字光标。
it("uses the receiving chart's candle position and clears through its action API", () => {
  const chart = {
    getDataList: () => [{ timestamp: 120, close: 13 }],
    getSize: () => ({ width: 300 }),
    convertToPixel: vi.fn(() => ({ x: 45, y: 90 })),
    executeAction: vi.fn(),
  } as unknown as Chart;

  applyLinkedCrosshair(chart, 120);
  expect(chart.convertToPixel).toHaveBeenCalledWith({ timestamp: 120, value: 13 }, { paneId: "candle_pane" });
  expect(chart.executeAction).toHaveBeenLastCalledWith("onCrosshairChange", { paneId: "candle_pane", x: 45, y: 90 });
  applyLinkedCrosshair(chart, null);
  expect(chart.executeAction).toHaveBeenLastCalledWith("onCrosshairChange", null);
});

it("converts the native crosshair action's pixel to a timestamp", () => {
  const chart = { convertFromPixel: vi.fn(() => [{ timestamp: 120 }]) } as unknown as Chart;
  expect(linkedCrosshairTime(chart, { x: 45, paneId: "candle_pane" })).toBe(120);
  expect(chart.convertFromPixel).toHaveBeenCalledWith([{ x: 45 }], { paneId: "candle_pane" });
});
