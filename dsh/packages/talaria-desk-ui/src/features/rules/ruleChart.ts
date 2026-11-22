import type { IndicatorName, RulePreviewIndicator, RulePreviewResponse } from "@talaria/contracts";
import type { IndicatorCreate } from "klinecharts";
import { INDICATOR_COLORS, INDICATOR_LABELS } from "../indicators/config";
import { CANDLE_PANE_ID } from "../market-chart/crosshair";

export const OVERLAY_TEMPLATE = "talaria_overlay";
export const STUDY_TEMPLATE = "talaria_study";

export function ruleIndicatorLabel({ operand }: RulePreviewIndicator): string {
  const name = operand.name as IndicatorName;
  const label = INDICATOR_LABELS[name] ?? operand.name;
  const params = operand.params;
  const length = params?.anchor === "session" ? "UTC 日界" : params?.anchor === "snapshot" ? (operand.window === "chart" ? "快照起点" : "预警窗口起点") : params?.period;
  const deviation = params?.std_dev == null ? "" : `, ${params.std_dev}σ`;
  const channel = operand.channel && operand.channel !== "value" ? ` · ${operand.channel}` : "";
  const legacyWindow = (operand.window ?? "alert") === "alert" && params?.anchor !== "session"
    ? ` · 最近 ${Math.max(600, (params?.period ?? 1) + 2)} 根` : "";
  return `${label}(${length ?? "默认"}${deviation})${channel}${legacyWindow}`;
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
        key: "value", title: "规则指标 ", type: "line",
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
    const label = trigger ? "触发预警" : "条件成立";
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
