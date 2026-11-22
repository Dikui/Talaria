import type { ActionType, Chart, Coordinate, Crosshair, KLineData } from "klinecharts";

export const CANDLE_PANE_ID = "candle_pane";
export const CROSSHAIR_ACTION = "onCrosshairChange";

export function linkedCrosshairTime(chart: Chart, action: unknown): number | null {
  const { x, paneId } = (action ?? {}) as Crosshair;
  if (typeof x !== "number") return null;
  const points = chart.convertFromPixel([{ x }], { paneId });
  return Array.isArray(points) ? points[0]?.timestamp ?? null : points.timestamp ?? null;
}

function candleIndex(bars: KLineData[], timestamp: number): number {
  let left = 0;
  let right = bars.length - 1;
  while (left <= right) {
    const middle = Math.floor((left + right) / 2);
    const time = bars[middle].timestamp;
    if (time === timestamp) return middle;
    if (time < timestamp) left = middle + 1;
    else right = middle - 1;
  }
  return -1;
}

export function applyLinkedCrosshair(chart: Chart, timestamp: number | null) {
  const bars = chart.getDataList();
  const dataIndex = timestamp === null ? -1 : candleIndex(bars, timestamp);
  const pane = { paneId: CANDLE_PANE_ID };
  const { x, y } = dataIndex >= 0
    ? chart.convertToPixel({ dataIndex, value: bars[dataIndex].close }, pane) as Partial<Coordinate>
    : {};
  const size = chart.getSize(CANDLE_PANE_ID, "main");
  if (size && x !== undefined && y !== undefined && x >= 0 && x <= size.width) {
    chart.executeAction(CROSSHAIR_ACTION, { ...pane, x, y });
  } else {
    // v10 accepts null at runtime to clear the crosshair without emitting an action.
    chart.executeAction(CROSSHAIR_ACTION, null as unknown as Crosshair);
  }
}

export function observeLocalCrosshair(chart: Chart, element: HTMLElement, send: (ts: number | null) => void) {
  let active = false;
  const move = (action: unknown) => {
    const timestamp = linkedCrosshairTime(chart, action);
    active = timestamp !== null;
    send(timestamp);
  };
  const leave = () => {
    if (!active) return;
    active = false;
    send(null);
  };
  const checkPlot = (event: Event) => {
    if (!active) return;
    // KLineChart 10 clears silently when the pointer leaves a plot for an axis or separator.
    const panes = chart.getPaneOptions();
    const overPlot = Array.isArray(panes) && panes.some(({ id }) =>
      id && id !== "x_axis_pane" && chart.getDom(id, "main")?.contains(event.target as Node),
    );
    if (!overPlot) leave();
  };
  chart.subscribeAction(CROSSHAIR_ACTION, move);
  element.addEventListener("pointermove", checkPlot, true);
  element.addEventListener("pointerleave", leave);
  element.addEventListener("pointercancel", leave);
  return () => {
    element.removeEventListener("pointermove", checkPlot, true);
    element.removeEventListener("pointerleave", leave);
    element.removeEventListener("pointercancel", leave);
    chart.unsubscribeAction(CROSSHAIR_ACTION, move);
    leave();
  };
}

export function observeLinkedCrosshair(chart: Chart, getTimestamp: () => number | null) {
  const actions: ActionType[] = ["onVisibleRangeChange", "onScroll", "onZoom", "onPaneDrag"];
  let frame: number | undefined;
  let projected: number | null = null;
  function refresh() {
    if (frame !== undefined || (projected === null && getTimestamp() === null)) return;
    frame = requestAnimationFrame(() => {
      frame = undefined;
      const timestamp = getTimestamp();
      if (timestamp !== null || projected !== null) applyLinkedCrosshair(chart, timestamp);
      projected = timestamp;
    });
  }
  actions.forEach((action) => chart.subscribeAction(action, refresh));
  return {
    refresh,
    close() {
      if (frame !== undefined) cancelAnimationFrame(frame);
      actions.forEach((action) => chart.unsubscribeAction(action, refresh));
    },
  };
}
