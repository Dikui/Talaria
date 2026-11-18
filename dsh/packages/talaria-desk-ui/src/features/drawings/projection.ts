import type { Chart } from "klinecharts";
import type { DrawingPoint } from "@talaria/contracts";

import { formatPrice, type PixelPoint } from "./geometry";

/**
 * KLineChart 像素投影：价格只在最后一跳转 number。
 * convertToPixel({absolute:true}) 的 y 已经是图表根坐标（含 candle pane 顶部偏移），
 * 覆盖层与 .market-chart 元素同原点，因此可直接用于 SVG。
 */

export interface ChartFrame {
  width: number;
  height: number;
  paneTop: number;
  paneHeight: number;
}

interface PixelCoordinate {
  x?: number;
  y?: number;
}

interface PixelValue {
  timestamp?: number;
  dataIndex?: number;
  value?: number;
}

function toArray<T>(value: T | T[] | null | undefined): T[] {
  if (value == null) return [];
  return Array.isArray(value) ? value : [value];
}

export function readChartFrame(chart: Chart): ChartFrame | null {
  const size = chart.getSize();
  const pane = chart.getSize("candle_pane");
  if (size == null || pane == null) return null;
  if (!Number.isFinite(size.width) || !Number.isFinite(size.height)) return null;
  return {
    width: size.width,
    height: size.height,
    paneTop: pane.top,
    paneHeight: pane.height,
  };
}

export function projectPoints(
  chart: Chart,
  points: readonly DrawingPoint[],
): Array<PixelPoint | null> {
  if (points.length === 0) return [];
  const raw = chart.convertToPixel(
    points.map((point) => ({ timestamp: point.ts, value: Number(point.price) })),
    { absolute: true },
  ) as PixelCoordinate | PixelCoordinate[];
  const coordinates = toArray(raw);
  return points.map((_, index) => {
    const coordinate = coordinates[index];
    if (
      coordinate == null ||
      typeof coordinate.x !== "number" ||
      typeof coordinate.y !== "number" ||
      !Number.isFinite(coordinate.x) ||
      !Number.isFinite(coordinate.y)
    ) {
      return null;
    }
    return { x: coordinate.x, y: coordinate.y };
  });
}

export interface PlotPoint {
  ts: number;
  price: string;
}

/** 指针像素 → 时间/价格 DecimalStr；无法映射（如左侧空白）时返回 null。 */
export function unprojectPoint(
  chart: Chart,
  x: number,
  y: number,
  pricePrecision: number,
): PlotPoint | null {
  const raw = chart.convertFromPixel([{ x, y }], { absolute: true }) as
    | PixelValue
    | PixelValue[];
  const plot = toArray(raw)[0];
  if (plot == null) return null;
  let timestamp = plot.timestamp;
  if (typeof timestamp !== "number" || !Number.isFinite(timestamp)) {
    if (typeof plot.dataIndex !== "number") return null;
    const bars = chart.getDataList();
    if (bars.length === 0) return null;
    const index = Math.min(Math.max(Math.round(plot.dataIndex), 0), bars.length - 1);
    timestamp = bars[index]?.timestamp;
  }
  if (typeof timestamp !== "number" || !Number.isFinite(timestamp)) return null;
  if (typeof plot.value !== "number" || !Number.isFinite(plot.value)) return null;
  return { ts: timestamp, price: formatPrice(plot.value, pricePrecision) };
}

/** 当前 candle pane 纵向覆盖的价格区间（用于 get_chart_view 的可见范围）。 */
export function visiblePriceWindow(
  chart: Chart,
  pricePrecision: number,
): { price_min: string; price_max: string } | null {
  const frame = readChartFrame(chart);
  if (frame == null) return null;
  const raw = chart.convertFromPixel(
    [
      { x: 0, y: frame.paneTop },
      { x: 0, y: frame.paneTop + frame.paneHeight },
    ],
    { absolute: true },
  ) as PixelValue | PixelValue[];
  const [top, bottom] = toArray(raw);
  if (
    top == null ||
    bottom == null ||
    typeof top.value !== "number" ||
    typeof bottom.value !== "number" ||
    !Number.isFinite(top.value) ||
    !Number.isFinite(bottom.value)
  ) {
    return null;
  }
  return {
    price_min: formatPrice(Math.min(top.value, bottom.value), pricePrecision),
    price_max: formatPrice(Math.max(top.value, bottom.value), pricePrecision),
  };
}

/** 与图表帧无关的稳定签名：可见范围/尺寸/最后一根 K 线变化时才重绘覆盖层。 */
export function frameSignature(chart: Chart): string {
  const frame = readChartFrame(chart);
  const range = chart.getVisibleRange();
  const bars = chart.getDataList();
  const last = bars[bars.length - 1];
  return [
    frame ? `${frame.width}x${frame.height}@${frame.paneTop}+${frame.paneHeight}` : "none",
    `${range.from}:${range.to}:${range.realFrom}:${range.realTo}`,
    `${bars.length}:${last?.timestamp ?? 0}:${last?.high ?? 0}:${last?.low ?? 0}:${last?.close ?? 0}`,
  ].join("|");
}
