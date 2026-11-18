import type { CompactBar } from "@talaria/contracts";
import type { KLineData, Period } from "klinecharts";

/**
 * DecimalString → number 的唯一转换点。
 *
 * KLineChart 的数据结构只接受 number；其余 UI 展示（价格精度、元数据）继续
 * 使用契约里的字符串，避免十进制精度在到达图表前被二次舍入。
 * FEED_GAP 柱没有 OHLC，不属于 dataList，直接返回 null，由调用方计数跳过。
 */
export function toKLineData(bar: CompactBar): KLineData | null {
  if (
    bar.status === "FEED_GAP" ||
    bar.open === null ||
    bar.high === null ||
    bar.low === null ||
    bar.close === null
  ) {
    return null;
  }
  return {
    timestamp: bar.timestamp,
    open: Number(bar.open),
    high: Number(bar.high),
    low: Number(bar.low),
    close: Number(bar.close),
    volume: bar.volume === null ? 0 : Number(bar.volume),
    turnover: bar.turnover === null ? 0 : Number(bar.turnover),
  };
}

export interface KLineSeries {
  /** 与 KLineChart dataList 一一对应的紧凑柱，升序且不含 FEED_GAP。 */
  points: KLineData[];
  /** 被跳过的断流柱数量；API 仍保留这些柱，图表只在状态栏提示。 */
  gapCount: number;
}

/** 将 API 的紧凑柱序列转换为 KLineChart dataList，并按时间升序稳定排序。 */
export function toKLineSeries(bars: CompactBar[]): KLineSeries {
  const sorted = [...bars].sort((left, right) => left.timestamp - right.timestamp);
  const points: KLineData[] = [];
  let gapCount = 0;
  for (const bar of sorted) {
    if (bar.status === "FEED_GAP") {
      gapCount += 1;
      continue;
    }
    const point = toKLineData(bar);
    if (point !== null) {
      points.push(point);
    }
  }
  return { points, gapCount };
}

/** 周期标识 → KLineChart Period。 */
export function toPeriod(timeframe: string): Period {
  const match = /^(\d+)([mhd])$/.exec(timeframe);
  if (!match) {
    throw new Error(`不支持的周期：${timeframe}`);
  }
  const span = Number(match[1]);
  switch (match[2]) {
    case "m":
      return { type: "minute", span };
    case "h":
      return { type: "hour", span };
    default:
      return { type: "day", span };
  }
}
