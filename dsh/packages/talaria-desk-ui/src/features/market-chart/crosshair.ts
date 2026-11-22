import type { Chart, Coordinate, Crosshair } from "klinecharts";

export const CANDLE_PANE_ID = "candle_pane";
export const CROSSHAIR_ACTION = "onCrosshairChange";

export function linkedCrosshairTime(chart: Chart, action: unknown): number | null {
  const { x, paneId } = (action ?? {}) as Crosshair;
  if (typeof x !== "number") return null;
  const points = chart.convertFromPixel([{ x }], { paneId });
  return Array.isArray(points) ? points[0]?.timestamp ?? null : points.timestamp ?? null;
}

export function applyLinkedCrosshair(chart: Chart, timestamp: number | null) {
  const candle = timestamp === null
    ? undefined
    : chart.getDataList().find((bar) => bar.timestamp === timestamp);
  const pane = { paneId: CANDLE_PANE_ID };
  const { x, y } = candle
    ? chart.convertToPixel({ timestamp: candle.timestamp, value: candle.close }, pane) as Partial<Coordinate>
    : {};
  const size = chart.getSize(CANDLE_PANE_ID, "main");
  if (size && x !== undefined && y !== undefined && x >= 0 && x <= size.width) {
    chart.executeAction(CROSSHAIR_ACTION, { ...pane, x, y });
  } else {
    // v10 accepts null at runtime to clear the crosshair without emitting an action.
    chart.executeAction(CROSSHAIR_ACTION, null as unknown as Crosshair);
  }
}
