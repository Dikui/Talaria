import type { DrawingKind, DrawingStyle } from "@talaria/contracts";

import { getLocale, type Locale } from "../../lib/i18n";

/** 工具栏顺序即交易员常用顺序。 */
export type DrawingToolId =
  | "select"
  | "horizontal_line"
  | "ray"
  | "segment"
  | "parallel_channel"
  | "fib_retracement"
  | "rectangle"
  | "text_label";

export interface DrawingToolDefinition {
  id: DrawingToolId;
  label: string;
  labelEn: string;
  kind: DrawingKind | null;
  /** 完成该图元需要的点击次数；select 为 0。 */
  anchors: number;
  hint: string;
  hintEn: string;
}

export const DRAWING_TOOLS: readonly DrawingToolDefinition[] = [
  {
    id: "horizontal_line",
    label: "水平线",
    labelEn: "Horizontal line",
    kind: "HORIZONTAL_LINE",
    anchors: 1,
    hint: "点击价格位置放置水平线",
    hintEn: "Click a price level to place a horizontal line",
  },
  {
    id: "ray",
    label: "射线",
    labelEn: "Ray",
    kind: "RAY",
    anchors: 2,
    hint: "点击起点，再点击方向",
    hintEn: "Click the start point, then the direction",
  },
  {
    id: "segment",
    label: "线段",
    labelEn: "Segment",
    kind: "SEGMENT",
    anchors: 2,
    hint: "点击起点与终点",
    hintEn: "Click the start and end points",
  },
  {
    id: "parallel_channel",
    label: "平行通道",
    labelEn: "Parallel channel",
    kind: "PARALLEL_CHANNEL",
    anchors: 3,
    hint: "点击基线两点，再点击通道宽度",
    hintEn: "Click two baseline points, then the channel width",
  },
  {
    id: "fib_retracement",
    label: "斐波那契",
    labelEn: "Fibonacci",
    kind: "FIB_RETRACEMENT",
    anchors: 2,
    hint: "点击回撤起点与终点",
    hintEn: "Click the retracement start and end points",
  },
  {
    id: "rectangle",
    label: "矩形区间",
    labelEn: "Rectangle zone",
    kind: "RECTANGLE",
    anchors: 2,
    hint: "拖出价格区间",
    hintEn: "Drag out the price zone",
  },
  {
    id: "text_label",
    label: "文本标签",
    labelEn: "Text label",
    kind: "TEXT_LABEL",
    anchors: 1,
    hint: "点击放置文本标注",
    hintEn: "Click to place a text annotation",
  },
] as const;

const BY_KIND = new Map<DrawingKind, DrawingToolDefinition>(
  DRAWING_TOOLS.map((tool) => [tool.kind as DrawingKind, tool]),
);

export const KIND_LABELS: Record<DrawingKind, string> = {
  HORIZONTAL_LINE: "水平线",
  RAY: "射线",
  SEGMENT: "线段",
  PARALLEL_CHANNEL: "平行通道",
  FIB_RETRACEMENT: "斐波那契回撤",
  RECTANGLE: "矩形区间",
  TEXT_LABEL: "文本标签",
};

export const KIND_LABELS_EN: Record<DrawingKind, string> = {
  HORIZONTAL_LINE: "Horizontal line",
  RAY: "Ray",
  SEGMENT: "Segment",
  PARALLEL_CHANNEL: "Parallel channel",
  FIB_RETRACEMENT: "Fibonacci retracement",
  RECTANGLE: "Rectangle zone",
  TEXT_LABEL: "Text label",
};

/** 运行时按语言取中文/英文，避免模块加载时求值。 */
export function drawingKindLabel(kind: DrawingKind, locale: Locale = getLocale()): string {
  return locale === "zh" ? KIND_LABELS[kind] : KIND_LABELS_EN[kind];
}

export function drawingToolLabel(
  tool: DrawingToolDefinition,
  locale: Locale = getLocale(),
): string {
  return locale === "zh" ? tool.label : tool.labelEn;
}

export function drawingToolHint(
  tool: DrawingToolDefinition,
  locale: Locale = getLocale(),
): string {
  return locale === "zh" ? tool.hint : tool.hintEn;
}

export function toolDefinition(toolId: DrawingToolId): DrawingToolDefinition | null {
  return DRAWING_TOOLS.find((tool) => tool.id === toolId) ?? null;
}

export function toolForKind(kind: DrawingKind): DrawingToolDefinition | null {
  return BY_KIND.get(kind) ?? null;
}

export function anchorsRequired(kind: DrawingKind): number {
  return toolForKind(kind)?.anchors ?? 0;
}

/** 画线默认样式：颜色/线宽/线型都由后端 ContractModel 校验。 */
export function defaultDrawingStyle(): DrawingStyle {
  return {
    color: "#4c8dff",
    line_width: 1,
    line_style: "solid",
    fill_opacity: 0.12,
  };
}

/** 新画线默认只在当前周期可见；空数组表示全周期。 */
export function visibleTimeframesFor(timeframe: string): string[] {
  return timeframe === "" ? [] : [timeframe];
}

export const DRAWING_COLORS = [
  "#4c8dff",
  "#f5a623",
  "#f2545b",
  "#37c38f",
  "#b46cf7",
  "#9aa4b2",
] as const;
