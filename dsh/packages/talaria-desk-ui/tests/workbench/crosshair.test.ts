import { expect, it, vi } from "vitest";
import type { Chart } from "klinecharts";
import { applyLinkedCrosshair, linkedCrosshairTime, observeLinkedCrosshair, observeLocalCrosshair } from "../../src/features/market-chart/crosshair";

// 平台 02：接收端用自己的坐标驱动 KLineChart 十字光标。
it("uses the receiving chart's candle position and clears through its action API", () => {
  const chart = {
    getDataList: () => [{ timestamp: 120, close: 13 }],
    getSize: () => ({ width: 300 }),
    convertToPixel: vi.fn(() => ({ x: 45, y: 90 })),
    executeAction: vi.fn(),
  } as unknown as Chart;

  applyLinkedCrosshair(chart, 120);
  expect(chart.convertToPixel).toHaveBeenCalledWith({ dataIndex: 0, value: 13 }, { paneId: "candle_pane" });
  expect(chart.executeAction).toHaveBeenLastCalledWith("onCrosshairChange", { paneId: "candle_pane", x: 45, y: 90 });
  applyLinkedCrosshair(chart, null);
  expect(chart.executeAction).toHaveBeenLastCalledWith("onCrosshairChange", null);
});

it("converts the native crosshair action's pixel to a timestamp", () => {
  const chart = { convertFromPixel: vi.fn(() => [{ timestamp: 120 }]) } as unknown as Chart;
  expect(linkedCrosshairTime(chart, { x: 45, paneId: "candle_pane" })).toBe(120);
  expect(chart.convertFromPixel).toHaveBeenCalledWith([{ x: 45 }], { paneId: "candle_pane" });
});

// 平台 02 审查：KLineChart 在价格轴上清除光标，但不发布清除 action。
it("sends a clear when the pointer moves from the plot to the price axis", () => {
  const element = new EventTarget();
  const send = vi.fn();
  const chart = {
    subscribeAction: vi.fn(),
    unsubscribeAction: vi.fn(),
    convertFromPixel: () => [{ timestamp: 120 }],
    getPaneOptions: () => [{ id: "candle_pane" }],
    getDom: () => ({ contains: () => false }),
  };
  const close = observeLocalCrosshair(chart as unknown as Chart, element as HTMLElement, send);
  chart.subscribeAction.mock.calls[0][1]({ x: 45, paneId: "candle_pane" });
  element.dispatchEvent(new Event("pointermove"));
  expect(send.mock.calls).toEqual([[120], [null]]);
  close();
});

it("reprojects the same time after bars load and after zooming or scrolling", () => {
  vi.useFakeTimers();
  vi.stubGlobal("requestAnimationFrame", (callback: () => void) => setTimeout(callback, 16));
  vi.stubGlobal("cancelAnimationFrame", clearTimeout);
  const actions = new Map<string, () => void>();
  let loaded = false;
  let x = 45;
  const chart = {
    getDataList: () => loaded ? [{ timestamp: 120, close: 13 }] : [],
    getSize: () => ({ width: 300 }),
    convertToPixel: () => ({ x, y: 90 }),
    executeAction: vi.fn(),
    subscribeAction: (type: string, callback: () => void) => actions.set(type, callback),
    unsubscribeAction: (type: string) => actions.delete(type),
  };
  const cursor = observeLinkedCrosshair(chart as unknown as Chart, () => 120);
  const lastProjection = () => chart.executeAction.mock.lastCall?.[1];
  try {
    cursor.refresh();
    vi.runAllTimers();
    expect(lastProjection()).toBeNull();

    loaded = true;
    cursor.refresh();
    vi.runAllTimers();
    expect(lastProjection()).toMatchObject({ x: 45 });
    x = 100;
    actions.get("onZoom")?.();
    vi.runAllTimers();
    expect(lastProjection()).toMatchObject({ x: 100 });
    x = 200;
    actions.get("onScroll")?.();
    vi.runAllTimers();
    expect(lastProjection()).toMatchObject({ x: 200 });
  } finally {
    cursor.close();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  }
});

// 平台 02 审查：历史分页后重投影仍只需检查对数数量的 K 线。
it("looks up the received candle by timestamp in logarithmic time", () => {
  let reads = 0;
  const bars = Array.from({ length: 4_096 }, (_, index) => ({
    get timestamp() { reads += 1; return index * 60; },
    close: index,
  }));
  const project = vi.fn(() => ({ x: 45, y: 90 }));
  const chart = {
    getDataList: () => bars,
    getSize: () => ({ width: 300 }),
    convertToPixel: project,
    executeAction: vi.fn(),
  } as unknown as Chart;
  applyLinkedCrosshair(chart, 3_072 * 60);
  expect(project).toHaveBeenCalledWith(expect.objectContaining({ value: 3_072 }), { paneId: "candle_pane" });
  expect(reads).toBeLessThanOrEqual(15);
});
