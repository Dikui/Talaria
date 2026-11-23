import type { IndicatorName, RulePreviewIndicator, RulePreviewResponse } from "@talaria/contracts";
import type { IndicatorCreate } from "klinecharts";
import { t } from "../../lib/i18n";
import { indicatorNameLabel } from "../alerts/model";
import { INDICATOR_COLORS } from "../indicators/config";
import { CANDLE_PANE_ID } from "../market-chart/crosshair";

export const OVERLAY_TEMPLATE = "talaria_overlay";
export const STUDY_TEMPLATE = "talaria_study";

function channelLabel(channel: string): string {
  switch (channel) {
    case "upper": return t("上轨", "Upper");
    case "lower": return t("下轨", "Lower");
    case "mid": return t("中轨", "Middle");
    default: return channel;
  }
}

export function ruleIndicatorLabel({ operand }: RulePreviewIndicator): string {
  const name = operand.name as IndicatorName;
  const label = indicatorNameLabel(name);
  const params = operand.params;
  const length = params?.anchor === "session"
    ? t("UTC 日界", "UTC day boundary")
    : params?.anchor === "snapshot"
      ? (operand.window === "chart" ? t("快照起点", "Snapshot start") : t("预警窗口起点", "Alert window start"))
      : params?.period;
  const deviation = params?.std_dev == null ? "" : `, ${params.std_dev}σ`;
  const channel = operand.channel && operand.channel !== "value"
    ? ` · ${channelLabel(operand.channel)}` : "";
  const legacyWindow = (operand.window ?? "alert") === "alert" && params?.anchor !== "session"
    ? t(` · 最近 ${Math.max(600, (params?.period ?? 1) + 2)} 根`, ` · last ${Math.max(600, (params?.period ?? 1) + 2)} bars`) : "";
  return `${label}(${length ?? t("默认", "Default")}${deviation})${channel}${legacyWindow}`;
}

export function ruleChartIndicators(
  preview: RulePreviewResponse | null | undefined,
  markerColor: string,
): IndicatorCreate[] {
  if (!preview) return [];
  const indicators: IndicatorCreate[] = preview.indicators.map((series) => {
    const isMain = series.pane === "main";
    const name = series.operand.name.replace(/^legacy_/, "").toUpperCase() as IndicatorName;
    const rows = new Map(series.points.map((point, index) => [point.timestamp, {
      value: point.values.value == null ? null : Number(point.values.value), index,
    }]));
    return {
      id: `talaria-rule-${JSON.stringify(series.operand)}`,
      name: isMain ? OVERLAY_TEMPLATE : STUDY_TEMPLATE,
      shortName: ruleIndicatorLabel(series),
      series: isMain ? "price" : "normal",
      paneId: isMain ? CANDLE_PANE_ID : undefined,
      figures: [{
        key: "value", title: t("规则指标 ", "Rule indicator "), type: "line",
        attrs: ({ data, coordinate }) => {
          const current = data.current as { index: number };
          const next = data.next as { index: number } | null;
          if (!next || next.index === current.index + 1) return null;
          const point = coordinate.current as { x: number; value: number };
          return { coordinates: [{ x: point.x, y: point.value }, { x: point.x, y: point.value }] };
        },
        styles: () => ({ color: INDICATOR_COLORS[name], style: "dashed" }),
      }],
      calc: async bars => bars.map(bar => rows.get(bar.timestamp) ?? { value: null, index: -1 }),
    };
  });
  for (const kind of ["condition_true", "would_trigger"] as const) {
    if (!preview[kind].length) continue;
    const trigger = kind === "would_trigger";
    const label = trigger ? t("触发预警", "Alert trigger") : t("条件成立", "Condition met");
    indicators.push({
      id: `talaria-rule-${kind}`,
      name: OVERLAY_TEMPLATE,
      series: "price",
      shortName: label,
      paneId: CANDLE_PANE_ID,
      figures: [{
        key: "signal", title: label, type: "circle",
        attrs: ({ coordinate, barSpace }) => ({
          y: (coordinate.current as Record<string, number>).signal + (trigger ? -8 : 8),
          r: Math.min(4, Math.max(2, barSpace.halfGapBar)),
        }),
        styles: () => ({ color: markerColor, style: trigger ? "fill" : "stroke" }),
      }],
      calc: async bars => {
        const matches = new Map(preview[kind].map(hit => [hit.timestamp, hit.price]));
        return bars.map(bar => {
          if (!matches.has(bar.timestamp)) return { signal: null };
          const price = matches.get(bar.timestamp);
          return { signal: price == null ? bar.close : Number(price) };
        });
      },
    });
  }
  return indicators;
}
