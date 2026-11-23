import type { DrawingKind } from "@talaria/contracts";

import { t } from "../../lib/i18n";

/**
 * 画线几何：只依赖像素坐标，不依赖 KLineChart 实例，便于纯函数测试。
 *
 * 价格/时间到像素的转换由调用方通过 chart.convertToPixel 完成；
 * 本模块只负责把锚点变成可渲染的线、矩形、多边形与贴边标签。
 */

export interface PixelPoint {
  x: number;
  y: number;
}

export interface PixelSize {
  width: number;
  height: number;
}

export interface DrawingAnchor extends PixelPoint {
  /** 供水平线/斐波那契标签展示的 DecimalStr；不参与几何计算。 */
  price?: string;
}

export interface GeometryLine {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  dashed: boolean;
}

export interface GeometryRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface GeometryPolygon {
  points: PixelPoint[];
}

export interface GeometryLabel {
  x: number;
  y: number;
  text: string;
  anchor: "start" | "middle" | "end";
  /** 估算宽度，用于命中测试与避免遮挡。 */
  width: number;
  height: number;
  role: "text" | "price" | "fib";
}

export interface DrawingGeometry {
  lines: GeometryLine[];
  rects: GeometryRect[];
  polygons: GeometryPolygon[];
  labels: GeometryLabel[];
  anchors: PixelPoint[];
}

export interface GeometryOptions {
  size: PixelSize;
  text?: string;
}

/** 与后端 DrawingKind 的斐波那契水平保持同序。 */
export const FIB_RATIOS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1] as const;

const LABEL_HEIGHT = 18;
const LABEL_PADDING = 8;
const EDGE_GAP = 6;

/** 固定小数位的 DecimalStr，避免把浮点误差写进画线坐标。 */
export function formatPrice(value: number, precision: number): string {
  const digits = Math.min(8, Math.max(0, Math.trunc(precision)));
  if (!Number.isFinite(value)) {
    return "0";
  }
  return value.toFixed(digits);
}

export function priceToNumber(price: string): number {
  return Number(price);
}

function isCjk(char: string): boolean {
  return (char.codePointAt(0) ?? 0) > 0xff;
}

export function estimateTextWidth(text: string): number {
  let width = LABEL_PADDING;
  for (const char of text) {
    width += isCjk(char) ? 12 : 7;
  }
  return width;
}

/** 超长文本截断并加省略号，避免在画布边缘被裁切。 */
export function fitText(text: string, maxWidth: number): string {
  if (estimateTextWidth(text) <= maxWidth) {
    return text;
  }
  const chars = [...text];
  let out = "";
  for (const char of chars) {
    if (estimateTextWidth(`${out}${char}…`) > maxWidth) {
      break;
    }
    out += char;
  }
  return out === "" ? "…" : `${out}…`;
}

export interface PlacedLabel {
  x: number;
  y: number;
  anchor: "start" | "middle" | "end";
  width: number;
  height: number;
}

/**
 * 文本贴边翻转：右侧放不下时改为右对齐，底部放不下时移到锚点上方。
 * 保证标签整体留在画布内（除非文本本身比画布更宽）。
 */
export function placeLabel(
  x: number,
  y: number,
  size: PixelSize,
  text: string,
): PlacedLabel {
  const width = estimateTextWidth(text);
  const height = LABEL_HEIGHT;
  let anchor: PlacedLabel["anchor"] = "start";
  let left = x;

  if (x + width > size.width - EDGE_GAP) {
    anchor = "end";
    left = Math.min(x, size.width - EDGE_GAP);
  }
  if (anchor === "start" && x - 0 < EDGE_GAP) {
    left = EDGE_GAP;
  }
  if (anchor === "end" && left - width < EDGE_GAP) {
    left = Math.max(EDGE_GAP + width, left);
    if (left > size.width - EDGE_GAP) {
      left = size.width - EDGE_GAP;
    }
  }

  let top = y;
  if (top + height > size.height - EDGE_GAP) {
    top = y - height - EDGE_GAP;
  }
  top = Math.max(EDGE_GAP, Math.min(top, size.height - height - EDGE_GAP));
  return { x: left, y: top, anchor, width, height };
}

export function distanceToSegment(
  px: number,
  py: number,
  x1: number,
  y1: number,
  x2: number,
  y2: number,
): number {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const lengthSquared = dx * dx + dy * dy;
  if (lengthSquared === 0) {
    return Math.hypot(px - x1, py - y1);
  }
  const t = Math.max(
    0,
    Math.min(1, ((px - x1) * dx + (py - y1) * dy) / lengthSquared),
  );
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}

function lineThrough(
  point: PixelPoint,
  direction: PixelPoint,
  size: PixelSize,
): [PixelPoint, PixelPoint] {
  const candidates: number[] = [0];
  if (Math.abs(direction.x) > 1e-9) {
    candidates.push((0 - point.x) / direction.x);
    candidates.push((size.width - point.x) / direction.x);
  }
  if (Math.abs(direction.y) > 1e-9) {
    candidates.push((0 - point.y) / direction.y);
    candidates.push((size.height - point.y) / direction.y);
  }
  const inBounds = candidates.filter((t) => {
    const x = point.x + direction.x * t;
    const y = point.y + direction.y * t;
    return (
      x >= -0.5 &&
      x <= size.width + 0.5 &&
      y >= -0.5 &&
      y <= size.height + 0.5
    );
  });
  const min = Math.min(...inBounds);
  const max = Math.max(...inBounds);
  return [
    { x: point.x + direction.x * min, y: point.y + direction.y * min },
    { x: point.x + direction.x * max, y: point.y + direction.y * max },
  ];
}

function rayEnd(
  origin: PixelPoint,
  direction: PixelPoint,
  size: PixelSize,
): PixelPoint {
  const candidates: number[] = [];
  if (Math.abs(direction.x) > 1e-9) {
    for (const boundary of [0, size.width]) {
      const t = (boundary - origin.x) / direction.x;
      if (t >= 0) candidates.push(t);
    }
  }
  if (Math.abs(direction.y) > 1e-9) {
    for (const boundary of [0, size.height]) {
      const t = (boundary - origin.y) / direction.y;
      if (t >= 0) candidates.push(t);
    }
  }
  if (candidates.length === 0) {
    return { x: origin.x, y: origin.y };
  }
  const t = Math.min(...candidates);
  return { x: origin.x + direction.x * t, y: origin.y + direction.y * t };
}

function label(
  placed: PlacedLabel,
  text: string,
  role: GeometryLabel["role"],
): GeometryLabel {
  return { ...placed, text, role };
}

function labelAt(
  x: number,
  y: number,
  size: PixelSize,
  text: string,
  role: GeometryLabel["role"],
): GeometryLabel {
  const fitted = fitText(text, Math.max(40, size.width - EDGE_GAP * 2));
  return label(placeLabel(x, y, size, fitted), fitted, role);
}

/**
 * 主入口：锚点不足时返回空几何；未知/缺点的画线不渲染，而不是抛错。
 */
export function buildDrawingGeometry(
  kind: DrawingKind,
  anchors: DrawingAnchor[],
  options: GeometryOptions,
): DrawingGeometry {
  const { size } = options;
  const geometry: DrawingGeometry = {
    lines: [],
    rects: [],
    polygons: [],
    labels: [],
    anchors: anchors.map(({ x, y }) => ({ x, y })),
  };
  const [a0, a1, a2] = anchors;

  switch (kind) {
    case "HORIZONTAL_LINE": {
      if (!a0) return geometry;
      geometry.lines.push({
        x1: 0,
        y1: a0.y,
        x2: size.width,
        y2: a0.y,
        dashed: false,
      });
      if (a0.price !== undefined) {
        geometry.labels.push(
          labelAt(
            size.width - EDGE_GAP,
            a0.y - LABEL_HEIGHT - 2,
            size,
            a0.price,
            "price",
          ),
        );
      }
      return geometry;
    }
    case "RAY": {
      if (!a0 || !a1) return geometry;
      const end = rayEnd(a0, { x: a1.x - a0.x, y: a1.y - a0.y }, size);
      geometry.lines.push({
        x1: a0.x,
        y1: a0.y,
        x2: end.x,
        y2: end.y,
        dashed: false,
      });
      return geometry;
    }
    case "SEGMENT": {
      if (!a0 || !a1) return geometry;
      geometry.lines.push({
        x1: a0.x,
        y1: a0.y,
        x2: a1.x,
        y2: a1.y,
        dashed: false,
      });
      return geometry;
    }
    case "PARALLEL_CHANNEL": {
      if (!a0 || !a1 || !a2) return geometry;
      const direction = { x: a1.x - a0.x, y: a1.y - a0.y };
      const lengthSquared = direction.x * direction.x + direction.y * direction.y;
      if (lengthSquared < 1e-9) return geometry;
      const t =
        ((a2.x - a0.x) * direction.x + (a2.y - a0.y) * direction.y) /
        lengthSquared;
      const projection = {
        x: a0.x + direction.x * t,
        y: a0.y + direction.y * t,
      };
      const offset = { x: a2.x - projection.x, y: a2.y - projection.y };
      const [b0, b1] = lineThrough(a0, direction, size);
      const c0 = { x: b0.x + offset.x, y: b0.y + offset.y };
      const c1 = { x: b1.x + offset.x, y: b1.y + offset.y };
      geometry.lines.push(
        { x1: b0.x, y1: b0.y, x2: b1.x, y2: b1.y, dashed: false },
        { x1: c0.x, y1: c0.y, x2: c1.x, y2: c1.y, dashed: true },
      );
      geometry.polygons.push({ points: [b0, b1, c1, c0] });
      return geometry;
    }
    case "FIB_RETRACEMENT": {
      if (!a0 || !a1) return geometry;
      const low = priceToNumber(a0.price ?? "0");
      const high = priceToNumber(a1.price ?? "0");
      for (const ratio of FIB_RATIOS) {
        const y = a1.y + (a0.y - a1.y) * ratio;
        geometry.lines.push({
          x1: 0,
          y1: y,
          x2: size.width,
          y2: y,
          dashed: ratio !== 0 && ratio !== 1,
        });
        const percent = `${(ratio * 100).toFixed(1)}%`;
        const price =
          a0.price !== undefined && a1.price !== undefined
            ? ` (${formatPrice(low + (high - low) * ratio, 2)})`
            : "";
        geometry.labels.push(
          labelAt(
            size.width - EDGE_GAP,
            y - LABEL_HEIGHT - 2,
            size,
            `${percent}${price}`,
            "fib",
          ),
        );
      }
      return geometry;
    }
    case "RECTANGLE": {
      if (!a0 || !a1) return geometry;
      const x = Math.min(a0.x, a1.x);
      const y = Math.min(a0.y, a1.y);
      geometry.rects.push({
        x,
        y,
        width: Math.abs(a1.x - a0.x),
        height: Math.abs(a1.y - a0.y),
      });
      return geometry;
    }
    case "TEXT_LABEL": {
      if (!a0) return geometry;
      geometry.labels.push(
        labelAt(a0.x, a0.y, size, options.text?.trim() || t("文本", "Text"), "text"),
      );
      return geometry;
    }
    default:
      return geometry;
  }
}
