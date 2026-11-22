import type { IndicatorName, RulePreviewIndicator, RulePreviewResponse } from "@talaria/contracts";
import type { IndicatorCreate, KLineData } from "klinecharts";
import { toIndicatorRow, type IndicatorValueMap } from "../../lib/indicatorData";
import { INDICATOR_COLORS, INDICATOR_LABELS } from "../indicators/config";
import { CANDLE_PANE_ID } from "../market-chart/crosshair";

export const OVERLAY_TEMPLATE = "talaria_overlay";
export const STUDY_TEMPLATE = "talaria_study";

export function ruleIndicatorLabel({ operand }: RulePreviewIndicator): string {
  const name = operand.name as IndicatorName;
  const label = INDICATOR_LABELS[name] ?? operand.name;
  const params = operand.params;
  const length = params?.anchor === "session" ? "UTC 日界" : params?.anchor === "snapshot" ? "快照起点" : params?.period;
  const deviation = params?.std_dev == null ? "" : `, ${params.std_dev}σ`;
  const channel = operand.channel && operand.channel !== "value" ? ` · ${operand.channel}` : "";
  return `${label}(${length ?? "默认"}${deviation})${channel}`;
}

function calcRows(values: IndicatorValueMap, channel: string) {
  return async (bars: KLineData[]) =>
    bars.map(({ timestamp }) => toIndicatorRow(timestamp, values, [channel]));
}

export function ruleChartIndicators(
  preview: RulePreviewResponse | null | undefined,
  markerColor: string,
): IndicatorCreate[] {
  if (!preview) return [];
  const indicators: IndicatorCreate[] = preview.indicators.map((series) => {
    const isMain = series.pane === "main";
    const name = series.operand.name.replace(/^legacy_/, "").toUpperCase() as IndicatorName;
    return {
      id: `talaria-rule-${JSON.stringify(series.operand)}`,
      name: isMain ? OVERLAY_TEMPLATE : STUDY_TEMPLATE,
      shortName: ruleIndicatorLabel(series),
      series: isMain ? "price" : "normal",
      paneId: isMain ? CANDLE_PANE_ID : undefined,
      figures: [{
        key: "value", title: "规则指标 ", type: "line",
        styles: () => ({ color: INDICATOR_COLORS[name], style: "dashed" }),
      }],
      calc: calcRows(new Map(series.points.map((point) => [point.timestamp, point.values])), "value"),
    };
  });
  if (preview.signals.length > 0) {
    indicators.push({
      id: "talaria-rule-signals",
      name: OVERLAY_TEMPLATE,
      series: "price",
      shortName: "规则信号",
      paneId: CANDLE_PANE_ID,
      figures: [{
        key: "signal", title: "命中 ", type: "circle",
        attrs: ({ coordinate, barSpace }) => ({
          y: (coordinate.current as Record<string, number>).signal - 7,
          r: Math.min(4, Math.max(2, barSpace.halfGapBar)),
        }),
        styles: () => ({ color: markerColor, style: "fill" }),
      }],
      calc: calcRows(new Map(preview.signals.map(({ timestamp, price }) => [timestamp, { signal: price }])), "signal"),
    });
  }
  return indicators;
}
