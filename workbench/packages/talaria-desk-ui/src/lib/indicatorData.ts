import type { OHLCV, SeriesPoint } from "@luxalgo/vela";
import type { IndicatorSeries } from "@talaria/contracts";

import { getLocale, t } from "./i18n";
import { INDICATOR_CHANNELS } from "../features/indicators/config";

export type IndicatorValueMap = Map<number, Record<string, string | null>>;

/**
 * Index backend pre-computed IndicatorSeries by timestamp while retaining DecimalString
 * until the final render boundary. ZERO indicator calculation happens on the frontend.
 */
export function buildIndicatorValueMap(series: IndicatorSeries): IndicatorValueMap {
  const map: IndicatorValueMap = new Map();
  if (!Array.isArray(series?.points)) {
    return map;
  }
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
  const first = Array.isArray(series?.points) ? series.points[0] : undefined;
  return first === undefined ? [] : Object.keys(first.values ?? {});
}

/**
 * Align authoritative backend indicator values to Vela's OHLCV bars array.
 * Warmup and FEED_GAP timestamps emit `value: null` (Vela whitespace gap).
 */
export function alignIndicatorChannelPoints(
  bars: readonly OHLCV[],
  series: IndicatorSeries,
  channel: string,
): SeriesPoint[] {
  const valueMap = buildIndicatorValueMap(series);
  return bars.map((bar) => {
    const raw = valueMap.get(bar.time)?.[channel];
    if (raw === null || raw === undefined) {
      return { time: bar.time, value: null };
    }
    const numeric = Number(raw);
    return {
      time: bar.time,
      value: Number.isFinite(numeric) ? numeric : null,
    };
  });
}

function trimNumber(value: number): string {
  return Number.isInteger(value)
    ? String(value)
    : value.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

export function describeParams(series: IndicatorSeries): string {
  const period = series.params.period;
  if (series.name === "BOLL") {
    return `BOLL(${period}, ${trimNumber(Number(series.params.std_dev ?? "2"))}σ)`;
  }
  if (series.name === "VWAP") {
    return `VWAP(${
      series.params.anchor === "session"
        ? t("UTC 日界", "UTC day boundary")
        : t("快照起点", "snapshot start")
    })`;
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
  const latest = series.summary?.latest ?? {};
  return channels
    .map((channel) =>
      channels.length === 1
        ? formatIndicatorValue(latest[channel])
        : `${channel} ${formatIndicatorValue(latest[channel])}`,
    )
    .join(" · ");
}
