import type { DrawingKind } from "@talaria/contracts";

import { getLocale, type Locale } from "../../lib/i18n";

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

export function drawingKindLabel(kind: DrawingKind, locale: Locale = getLocale()): string {
  return locale === "zh" ? KIND_LABELS[kind] : KIND_LABELS_EN[kind];
}
