import type { IndicatorSeries } from "@talaria/contracts";

import { getLocale, t } from "./i18n";
import { INDICATOR_CHANNELS } from "../features/indicators/config";

export type IndicatorValueMap = Map<number, Record<string, string | null>>;
export type IndicatorRow = Record<string, number | null>;

/**
 * 后端 IndicatorSeries → timestamp 索引。
 *
 * 这里刻意保留 DecimalString，不提前 Number：唯一转换发生在
 * `alignIndicatorRows`/`toIndicatorRow`，与 K 线 dataList 的最后一跳一致。
 */
export function buildIndicatorValueMap(series: IndicatorSeries): IndicatorValueMap {
  const map: IndicatorValueMap = new Map();
  for (const point of series.points) {
    map.set(point.timestamp, point.values);
  }
  return map;
}

export function channelsFor(series: IndicatorSeries): string[] {
  const configured = INDICATOR_CHANNELS[series.name];
  if (configured !== undefined) {
    return configured;
  }
  const first = series.points[0];
  return first === undefined ? [] : Object.keys(first.values);
}

/** 把一根 dataList 时间戳上的指标值转成 KLineChart calc 行；缺失/断流保持 null。 */
export function toIndicatorRow(
  timestamp: number,
  map: IndicatorValueMap,
  channels: string[],
): IndicatorRow {
  const values = map.get(timestamp);
  const row: IndicatorRow = {};
  for (const channel of channels) {
    const raw = values?.[channel];
    row[channel] = raw === null || raw === undefined ? null : Number(raw);
  }
  return row;
}

/** 严格按 dataList 顺序和长度对齐；没有对应 timestamp 的位置为 null，不补 0。 */
export function alignIndicatorRows(
  dataList: readonly { timestamp: number }[],
  series: IndicatorSeries,
): IndicatorRow[] {
  const map = buildIndicatorValueMap(series);
  const channels = channelsFor(series);
  return dataList.map((data) => toIndicatorRow(data.timestamp, map, channels));
}

function trimNumber(value: number): string {
  return Number.isInteger(value)
    ? String(value)
    : value.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

/** 图例里的参数文案；数值仅用于展示，不参与图表计算。 */
export function describeParams(series: IndicatorSeries): string {
  const period = series.params.period;
  if (series.name === "BOLL") {
    return `BOLL(${period}, ${trimNumber(Number(series.params.std_dev ?? "2"))}σ)`;
  }
  if (series.name === "VWAP") {
    return `VWAP(${series.params.anchor === "session" ? t("UTC 日界", "UTC day boundary") : t("快照起点", "snapshot start")})`;
  }
  return `${series.name}(${period})`;
}

export function formatIndicatorValue(
  value: string | number | null | undefined,
  digits = 2,
): string {
  if (value === null || value === undefined) {
    return "—";
  }
  const number = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(number)) {
    return "—";
  }
  return new Intl.NumberFormat(getLocale(), {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(number);
}

export function describeLatest(series: IndicatorSeries): string {
  const channels = channelsFor(series);
  return channels
    .map((channel) =>
      channels.length === 1
        ? formatIndicatorValue(series.summary.latest[channel])
        : `${channel} ${formatIndicatorValue(series.summary.latest[channel])}`,
    )
    .join(" · ");
}
