import type { DecimalString } from "@talaria/contracts";

import { formatPrice } from "./geometry";

/** KLineChart 的 getVisibleRange 返回值里只用这两个字段。 */
export interface VisibleRange {
  from: number;
  to: number;
}

export interface TimestampedBar {
  timestamp: number;
}

export interface TimeWindow {
  from_ts: number;
  to_ts: number;
}

export interface PriceWindow {
  price_min: DecimalString;
  price_max: DecimalString;
}

/**
 * 可视数据索引 → 后端可见范围时间戳。
 * `from`/`to` 允许越界（图表左右留白）或反向，这里归一化到已加载数据。
 */
export function timeWindow(
  range: VisibleRange,
  bars: TimestampedBar[],
): TimeWindow | null {
  if (bars.length === 0) {
    return null;
  }
  const last = bars.length - 1;
  const clamp = (index: number): number =>
    Math.max(0, Math.min(last, Math.trunc(index)));
  const fromIndex = clamp(range.from);
  const toIndex = clamp(range.to);
  const low = Math.min(fromIndex, toIndex);
  const high = Math.max(fromIndex, toIndex);
  return {
    from_ts: bars[low].timestamp,
    to_ts: bars[high].timestamp,
  };
}

/**
 * 主图窗格上下沿的价格 → DecimalStr 窗口。
 * 价格到像素的转换在 KLineChart 里完成；这一步只做最后一跳的字符串化。
 */
export function priceWindow(
  topPrice: number,
  bottomPrice: number,
  precision: number,
): PriceWindow | null {
  if (!Number.isFinite(topPrice) || !Number.isFinite(bottomPrice)) {
    return null;
  }
  return {
    price_min: formatPrice(Math.min(topPrice, bottomPrice), precision),
    price_max: formatPrice(Math.max(topPrice, bottomPrice), precision),
  };
}
