import {
  registerNativeIndicator,
  registerRendererLayer,
  type NativeIndicator,
  type NativeIndicatorContext,
  type RendererLayerArgs,
  type RendererLayerInstance,
} from "@luxalgo/vela/plugin";
import type {
  AgentAnnotationRecord,
  DraftRecord,
  KeyLevel,
  PriceScaleMode,
  SlopeLineGeometry,
  StructureAnchor,
  StructureFeature,
} from "@talaria/contracts";

type LayerCoordinateSystem = RendererLayerArgs["coords"];
type LayerPriceScale = RendererLayerArgs["scale"];
type LayerPaneBounds = RendererLayerArgs["bounds"];

import {
  annotationSummary,
  annotationZoneEnd,
  timeframeMs,
} from "../features/annotations/model";
import {
  categoryMeta,
  isZoneLevel,
  levelLabel,
  levelPrice,
} from "../features/key-levels/model";
import {
  familyMeta,
  structureLabel,
} from "../features/structures/model";
import type { ExecutionBarrierItem } from "./adapter";
import {
  layoutBarrierTargets,
  type BarrierDragOverlay,
  type BarrierHoverState,
} from "./barrierDragController";

export const LAYER_SYSTEM_LEVEL = "SYSTEM_LEVEL";
export const LAYER_SYSTEM_STRUCTURE = "SYSTEM_STRUCTURE";
export const LAYER_AGENT_ANNOTATION = "AGENT_ANNOTATION";
export const LAYER_EXECUTION_BARRIER = "EXECUTION_BARRIER";
export const LAYER_STAGED_DRAFT = "STAGED_DRAFT";

export interface SystemLevelLayerData {
  levels: KeyLevel[];
  precision: number;
  highlightId?: string | null;
}

export interface SystemStructureLayerData {
  structures: StructureFeature[];
  precision: number;
  highlightId?: string | null;
}

export interface AgentAnnotationLayerData {
  annotations: AgentAnnotationRecord[];
  timeframe: string;
  highlightId?: string | null;
}

export interface ExecutionBarrierLayerData {
  barriers: ExecutionBarrierItem[];
  dragOverlay?: BarrierDragOverlay | null;
  hover?: BarrierHoverState | null;
  readOnly?: boolean;
  replayActive?: boolean;
}

export interface StagedDraftLayerData {
  drafts: DraftRecord[];
  precision: number;
  highlightId?: string | null;
}

export interface ProjectedCoordinateContext {
  coords: Pick<
    LayerCoordinateSystem,
    "timeToX" | "priceToY" | "yToPrice" | "xToLogical" | "logicalToTime" | "width" | "height"
  >;
  scale: LayerPriceScale;
  bounds: LayerPaneBounds;
}

export function isNonTerminalDraft(draft: DraftRecord): boolean {
  return draft.status !== "DISCARDED" && draft.status !== "EXPIRED" && !draft.receipt;
}

const channelPushers = new Map<string, (payload: unknown) => void>();
const pendingLayerPayloads = new Map<string, unknown>();
const latestProjectionByChart = new Map<string, ProjectedCoordinateContext>();
let registered = false;

export function setChartLayerChannelData(
  chartId: string,
  layerId: string,
  payload: unknown,
): void {
  const key = `${chartId}:${layerId}`;
  pendingLayerPayloads.set(key, payload);
  const push = channelPushers.get(key);
  if (push) {
    push(payload);
  }
}

export function getLatestProjection(chartId: string): ProjectedCoordinateContext | null {
  return latestProjectionByChart.get(chartId) ?? null;
}

export function clearChartLayerState(chartId: string): void {
  latestProjectionByChart.delete(chartId);
  for (const key of [
    LAYER_SYSTEM_LEVEL,
    LAYER_SYSTEM_STRUCTURE,
    LAYER_AGENT_ANNOTATION,
    LAYER_EXECUTION_BARRIER,
    LAYER_STAGED_DRAFT,
  ]) {
    channelPushers.delete(`${chartId}:${key}`);
    pendingLayerPayloads.delete(`${chartId}:${key}`);
  }
}

const HIT_TOLERANCE_PX = 8;

/**
 * Pure hit-test against strictly read-only SYSTEM_LEVEL items using Vela's CoordinateSystem.
 */
export function hitTestSystemLevel(
  levels: readonly KeyLevel[],
  cursorY: number,
  coords: Pick<LayerCoordinateSystem, "priceToY">,
  scale: LayerPriceScale,
  bounds: LayerPaneBounds,
  tolerancePx = HIT_TOLERANCE_PX,
): KeyLevel | null {
  if (cursorY < bounds.top || cursorY > bounds.top + bounds.height) {
    return null;
  }
  let best: { level: KeyLevel; dist: number } | null = null;
  for (const level of levels) {
    if (isZoneLevel(level)) {
      const yTop = coords.priceToY(Number(level.price_high), scale, bounds);
      const yBottom = coords.priceToY(Number(level.price_low), scale, bounds);
      const minY = Math.min(yTop, yBottom) - tolerancePx * 0.5;
      const maxY = Math.max(yTop, yBottom) + tolerancePx * 0.5;
      if (cursorY >= minY && cursorY <= maxY) {
        const mid = (minY + maxY) / 2;
        const dist = Math.abs(cursorY - mid);
        if (!best || dist < best.dist) {
          best = { level, dist };
        }
      }
      continue;
    }
    const y = coords.priceToY(levelPrice(level), scale, bounds);
    if (!Number.isFinite(y)) continue;
    const dist = Math.abs(cursorY - y);
    if (dist <= tolerancePx && (!best || dist < best.dist)) {
      best = { level, dist };
    }
  }
  return best?.level ?? null;
}

/**
 * Evaluate slope line price at timestamp `ts` using LINEAR or LOG interpolation.
 */
export function slopePriceAtTs(
  anchorStart: StructureAnchor,
  anchorEnd: StructureAnchor,
  scaleMode: PriceScaleMode = "LINEAR",
  ts: number,
): number | null {
  const p1 = Number(anchorStart.price);
  const p2 = Number(anchorEnd.price);
  if (!Number.isFinite(p1) || !Number.isFinite(p2)) return null;
  if (anchorEnd.ts === anchorStart.ts) return p1;
  const ratio = (ts - anchorStart.ts) / (anchorEnd.ts - anchorStart.ts);
  if (scaleMode === "LOG") {
    if (p1 <= 0 || p2 <= 0) return null;
    return Math.exp(Math.log(p1) + (Math.log(p2) - Math.log(p1)) * ratio);
  }
  return p1 + (p2 - p1) * ratio;
}

function pointToSegmentDist(
  px: number,
  py: number,
  ax: number,
  ay: number,
  bx: number,
  by: number,
): number {
  const dx = bx - ax;
  const dy = by - ay;
  const lenSq = dx * dx + dy * dy;
  if (lenSq <= 1e-9) return Math.hypot(px - ax, py - ay);
  const t = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / lenSq));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

function slopeHitDistance(
  line: SlopeLineGeometry,
  cursorX: number,
  cursorY: number,
  coords: Pick<LayerCoordinateSystem, "timeToX" | "priceToY" | "width">,
  scale: LayerPriceScale,
  bounds: LayerPaneBounds,
): number | null {
  const x1 = coords.timeToX(line.anchor_start.ts);
  const x2 = coords.timeToX(line.anchor_end.ts);
  const y1 = coords.priceToY(Number(line.anchor_start.price), scale, bounds);
  const y2 = coords.priceToY(Number(line.anchor_end.price), scale, bounds);
  if (!Number.isFinite(x1) || !Number.isFinite(x2) || !Number.isFinite(y1) || !Number.isFinite(y2)) {
    return null;
  }
  const endTs = (() => {
    if (!line.extend_right || x2 <= x1 || coords.width <= x2) return line.anchor_end.ts;
    const msPerPx = (line.anchor_end.ts - line.anchor_start.ts) / (x2 - x1);
    return line.anchor_end.ts + msPerPx * (coords.width - x2);
  })();
  const steps = line.scale === "LOG" ? 8 : 1;
  let minDist = Infinity;
  for (let i = 0; i < steps; i += 1) {
    const tA = line.anchor_start.ts + ((endTs - line.anchor_start.ts) * i) / steps;
    const tB = line.anchor_start.ts + ((endTs - line.anchor_start.ts) * (i + 1)) / steps;
    const pA = slopePriceAtTs(line.anchor_start, line.anchor_end, line.scale, tA);
    const pB = slopePriceAtTs(line.anchor_start, line.anchor_end, line.scale, tB);
    if (pA === null || pB === null) continue;
    const xa = coords.timeToX(tA);
    const ya = coords.priceToY(pA, scale, bounds);
    const xb = coords.timeToX(tB);
    const yb = coords.priceToY(pB, scale, bounds);
    if (!Number.isFinite(ya) || !Number.isFinite(yb)) continue;
    minDist = Math.min(minDist, pointToSegmentDist(cursorX, cursorY, xa, ya, xb, yb));
  }
  return Number.isFinite(minDist) ? minDist : null;
}

/**
 * Pure hit-test against read-only SYSTEM_STRUCTURE items across all 4 geometry primitives.
 */
export function hitTestSystemStructure(
  structures: readonly StructureFeature[],
  cursorX: number,
  cursorY: number,
  coords: Pick<LayerCoordinateSystem, "timeToX" | "priceToY" | "width">,
  scale: LayerPriceScale,
  bounds: LayerPaneBounds,
  tolerancePx = HIT_TOLERANCE_PX,
): StructureFeature | null {
  if (cursorY < bounds.top || cursorY > bounds.top + bounds.height) {
    return null;
  }
  let best: { feature: StructureFeature; dist: number } | null = null;
  for (const feature of structures) {
    const g = feature.geometry;
    if (g.kind === "HORIZONTAL_LINE") {
      const y = coords.priceToY(Number(g.price), scale, bounds);
      if (!Number.isFinite(y)) continue;
      const dist = Math.abs(cursorY - y);
      if (dist <= tolerancePx && (!best || dist < best.dist)) {
        best = { feature, dist };
      }
    } else if (g.kind === "HORIZONTAL_ZONE") {
      const yTop = coords.priceToY(Number(g.price_high), scale, bounds);
      const yBottom = coords.priceToY(Number(g.price_low), scale, bounds);
      const minY = Math.min(yTop, yBottom) - tolerancePx * 0.5;
      const maxY = Math.max(yTop, yBottom) + tolerancePx * 0.5;
      const minX = g.start_ts != null ? coords.timeToX(g.start_ts) - 4 : -Infinity;
      const maxX = g.end_ts != null ? coords.timeToX(g.end_ts) + 4 : Infinity;
      if (cursorX >= minX && cursorX <= maxX && cursorY >= minY && cursorY <= maxY) {
        const dist = Math.abs(cursorY - (minY + maxY) / 2);
        if (!best || dist < best.dist) {
          best = { feature, dist };
        }
      }
    } else if (g.kind === "SLOPE_LINE") {
      const dist = slopeHitDistance(g, cursorX, cursorY, coords, scale, bounds);
      if (dist !== null && dist <= tolerancePx && (!best || dist < best.dist)) {
        best = { feature, dist };
      }
    } else if (g.kind === "CHANNEL") {
      const distUpper = slopeHitDistance(g.upper, cursorX, cursorY, coords, scale, bounds);
      const distLower = slopeHitDistance(g.lower, cursorX, cursorY, coords, scale, bounds);
      const railDist = Math.min(distUpper ?? Infinity, distLower ?? Infinity);
      if (railDist <= tolerancePx && (!best || railDist < best.dist)) {
        best = { feature, dist: railDist };
        continue;
      }
      const x1 = coords.timeToX(Math.min(g.upper.anchor_start.ts, g.lower.anchor_start.ts));
      const x2 = coords.timeToX(Math.max(g.upper.anchor_end.ts, g.lower.anchor_end.ts));
      const extend = g.upper.extend_right || g.lower.extend_right;
      if (cursorX >= x1 - 4 && (extend ? cursorX <= coords.width : cursorX <= x2 + 4) && x2 > x1) {
        const tsApprox =
          g.upper.anchor_start.ts +
          ((cursorX - coords.timeToX(g.upper.anchor_start.ts)) /
            Math.max(1e-6, coords.timeToX(g.upper.anchor_end.ts) - coords.timeToX(g.upper.anchor_start.ts))) *
            (g.upper.anchor_end.ts - g.upper.anchor_start.ts);
        const pU = slopePriceAtTs(g.upper.anchor_start, g.upper.anchor_end, g.upper.scale, tsApprox);
        const pL = slopePriceAtTs(g.lower.anchor_start, g.lower.anchor_end, g.lower.scale, tsApprox);
        if (pU !== null && pL !== null) {
          const yU = coords.priceToY(pU, scale, bounds);
          const yL = coords.priceToY(pL, scale, bounds);
          const lo = Math.min(yU, yL);
          const hi = Math.max(yU, yL);
          if (cursorY >= lo && cursorY <= hi) {
            const dist = Math.abs(cursorY - (lo + hi) / 2);
            if (!best || dist < best.dist) {
              best = { feature, dist };
            }
          }
        }
      }
    }
  }
  return best?.feature ?? null;
}

/**
 * Pure hit-test against read-only AGENT_ANNOTATION items using Vela's CoordinateSystem.
 */
export function hitTestAgentAnnotation(
  annotations: readonly AgentAnnotationRecord[],
  cursorX: number,
  cursorY: number,
  timeframe: string,
  coords: Pick<LayerCoordinateSystem, "timeToX" | "priceToY" | "width">,
  scale: LayerPriceScale,
  bounds: LayerPaneBounds,
  tolerancePx = HIT_TOLERANCE_PX,
): AgentAnnotationRecord | null {
  if (cursorY < bounds.top || cursorY > bounds.top + bounds.height) {
    return null;
  }
  const tfMs = timeframeMs(timeframe);
  for (const annotation of annotations) {
    if (annotation.status !== "ACTIVE") continue;
    const startX = coords.timeToX(annotation.ts_anchor);
    if (annotation.kind === "ZONE") {
      const endX = coords.timeToX(annotationZoneEnd(annotation, tfMs));
      const y1 = coords.priceToY(Number(annotation.price_high), scale, bounds);
      const y2 = coords.priceToY(Number(annotation.price_low), scale, bounds);
      const minX = Math.min(startX, endX) - 4;
      const maxX = Math.max(startX, endX) + 4;
      const minY = Math.min(y1, y2) - tolerancePx * 0.5;
      const maxY = Math.max(y1, y2) + tolerancePx * 0.5;
      if (cursorX >= minX && cursorX <= maxX && cursorY >= minY && cursorY <= maxY) {
        return annotation;
      }
    } else {
      const y = coords.priceToY(Number(annotation.price_low), scale, bounds);
      if (cursorX >= startX - 6 && cursorX <= coords.width && Math.abs(cursorY - y) <= tolerancePx) {
        return annotation;
      }
    }
  }
  return null;
}

/**
 * Pure hit-test against read-only non-terminal STAGED_DRAFT items using Vela's CoordinateSystem.
 * Checks entry band, invalidation stop, and all 1..4 take-profit lines.
 */
export function hitTestStagedDraft(
  drafts: readonly DraftRecord[],
  cursorY: number,
  coords: Pick<LayerCoordinateSystem, "priceToY">,
  scale: LayerPriceScale,
  bounds: LayerPaneBounds,
  tolerancePx = HIT_TOLERANCE_PX,
): DraftRecord | null {
  if (cursorY < bounds.top || cursorY > bounds.top + bounds.height) {
    return null;
  }
  let best: { draft: DraftRecord; dist: number } | null = null;
  for (const draft of drafts) {
    if (!isNonTerminalDraft(draft)) continue;
    const { entry_low, entry_high, invalidation_stop, take_profit } = draft.content;
    const yHigh = coords.priceToY(Number(entry_high), scale, bounds);
    const yLow = coords.priceToY(Number(entry_low), scale, bounds);
    if (Number.isFinite(yHigh) && Number.isFinite(yLow)) {
      const minY = Math.min(yHigh, yLow) - tolerancePx * 0.5;
      const maxY = Math.max(yHigh, yLow) + tolerancePx * 0.5;
      if (cursorY >= minY && cursorY <= maxY) {
        const dist = Math.abs(cursorY - (minY + maxY) / 2);
        if (!best || dist < best.dist) {
          best = { draft, dist };
        }
      }
    }
    const candidatePrices = [invalidation_stop, ...take_profit];
    for (const rawPrice of candidatePrices) {
      const y = coords.priceToY(Number(rawPrice), scale, bounds);
      if (!Number.isFinite(y)) continue;
      const dist = Math.abs(cursorY - y);
      if (dist <= tolerancePx && (!best || dist < best.dist)) {
        best = { draft, dist };
      }
    }
  }
  return best?.draft ?? null;
}

function withCanvas2d(
  canvas: HTMLCanvasElement | null,
  args: RendererLayerArgs,
  draw: (ctx: CanvasRenderingContext2D) => void,
): void {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const dpr = args.coords.dpr || 1;
  ctx.save();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.scale(dpr, dpr);
  ctx.beginPath();
  ctx.rect(0, args.bounds.top, args.coords.width, args.bounds.height);
  ctx.clip();
  draw(ctx);
  ctx.restore();
}

function drawPill(
  ctx: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  bg: string,
  fg: string,
  maxCanvasWidth?: number,
): void {
  ctx.font = "600 10px system-ui, -apple-system, sans-serif";
  const width = Math.ceil(ctx.measureText(text).width) + 12;
  const height = 18;
  const top = y - height / 2;
  const clampedX =
    maxCanvasWidth !== undefined
      ? Math.max(8, Math.min(x, maxCanvasWidth - width - 8))
      : x;
  ctx.fillStyle = bg;
  ctx.beginPath();
  ctx.roundRect(clampedX, top, width, height, 4);
  ctx.fill();
  ctx.fillStyle = fg;
  ctx.textBaseline = "middle";
  ctx.fillText(text, clampedX + 6, y);
}

function strokeSlopeGeometry(
  ctx: CanvasRenderingContext2D,
  line: SlopeLineGeometry,
  coords: LayerCoordinateSystem,
  scale: LayerPriceScale,
  bounds: LayerPaneBounds,
): { endX: number; endY: number } | null {
  const x1 = coords.timeToX(line.anchor_start.ts);
  const x2 = coords.timeToX(line.anchor_end.ts);
  if (!Number.isFinite(x1) || !Number.isFinite(x2)) return null;
  const endTs =
    line.extend_right && x2 > x1 && coords.width > x2
      ? line.anchor_end.ts + ((line.anchor_end.ts - line.anchor_start.ts) / (x2 - x1)) * (coords.width - x2)
      : line.anchor_end.ts;
  const steps = line.scale === "LOG" ? 16 : 1;
  ctx.beginPath();
  let lastX = x1;
  let lastY = 0;
  let started = false;
  for (let i = 0; i <= steps; i += 1) {
    const ts = line.anchor_start.ts + ((endTs - line.anchor_start.ts) * i) / steps;
    const price = slopePriceAtTs(line.anchor_start, line.anchor_end, line.scale, ts);
    if (price === null) continue;
    const x = coords.timeToX(ts);
    const y = coords.priceToY(price, scale, bounds);
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    if (!started) {
      ctx.moveTo(x, y);
      started = true;
    } else {
      ctx.lineTo(x, y);
    }
    lastX = x;
    lastY = y;
  }
  if (!started) return null;
  ctx.stroke();
  return { endX: lastX, endY: lastY };
}

function createSystemStructureLayer(): RendererLayerInstance {
  let canvas: HTMLCanvasElement | null = null;
  let lastChartId: string | null = null;

  return {
    mount(el) {
      canvas = el;
    },
    render(args) {
      const payload = args.data as (SystemStructureLayerData & { chartId?: string }) | undefined;
      if (payload?.chartId) {
        lastChartId = payload.chartId;
        latestProjectionByChart.set(payload.chartId, {
          coords: args.coords,
          scale: args.scale,
          bounds: args.bounds,
        });
      }
      withCanvas2d(canvas, args, (ctx) => {
        if (!payload || payload.structures.length === 0) return;
        const hovered =
          args.cursor !== null
            ? hitTestSystemStructure(
                payload.structures,
                args.cursor.x,
                args.cursor.y,
                args.coords,
                args.scale,
                args.bounds,
              )
            : null;
        const width = args.coords.width;
        const topBound = args.bounds.top;
        const bottomBound = args.bounds.top + args.bounds.height;

        for (const feature of payload.structures) {
          const meta = familyMeta(feature.family);
          const isHighlight =
            hovered?.structure_id === feature.structure_id ||
            payload.highlightId === feature.structure_id;
          const isEstimated = (feature.evidence_class ?? "OBSERVED") === "ESTIMATED";
          const label = structureLabel(feature, payload.precision);
          const g = feature.geometry;

          if (g.kind === "HORIZONTAL_LINE") {
            const y = args.coords.priceToY(Number(g.price), args.scale, args.bounds);
            if (!Number.isFinite(y)) continue;
            ctx.strokeStyle = meta.color;
            ctx.lineWidth = isHighlight ? 2 : 1.2;
            ctx.globalAlpha = feature.status === "BROKEN" ? 0.45 : isHighlight ? 1 : 0.8;
            ctx.setLineDash(isEstimated ? [3, 4] : feature.status === "ACTIVE" ? [6, 4] : [2, 3]);
            ctx.beginPath();
            ctx.moveTo(0, y);
            ctx.lineTo(width, y);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.globalAlpha = 1;
            if (isHighlight || feature.strength >= 0.6) {
              drawPill(ctx, label, 10, y, meta.color, "#0b1016", width);
            }
          } else if (g.kind === "HORIZONTAL_ZONE") {
            const yTop = args.coords.priceToY(Number(g.price_high), args.scale, args.bounds);
            const yBottom = args.coords.priceToY(Number(g.price_low), args.scale, args.bounds);
            const top = Math.min(yTop, yBottom);
            const h = Math.max(3, Math.abs(yBottom - yTop));
            const xStart = g.start_ts != null ? Math.max(0, args.coords.timeToX(g.start_ts)) : 0;
            const xEnd = g.end_ts != null ? Math.min(width, args.coords.timeToX(g.end_ts)) : width;
            const w = Math.max(4, xEnd - xStart);
            const baseAlpha =
              feature.family === "CONFLUENCE" ? 0.1 + feature.strength * 0.24 : 0.12;
            ctx.fillStyle = meta.color;
            ctx.globalAlpha = isHighlight ? Math.min(0.45, baseAlpha + 0.15) : baseAlpha;
            ctx.fillRect(xStart, top, w, h);
            ctx.globalAlpha = isHighlight ? 0.95 : 0.65;
            ctx.strokeStyle = meta.color;
            ctx.lineWidth = isHighlight ? 1.5 : 1;
            ctx.setLineDash(isEstimated ? [4, 4] : []);
            ctx.strokeRect(xStart, top, w, h);
            ctx.setLineDash([]);
            ctx.globalAlpha = 1;
            if (isHighlight || feature.family === "CONFLUENCE" || feature.strength >= 0.65) {
              drawPill(ctx, label, width, top + h / 2, meta.color, "#0b1016", width);
            }
          } else if (g.kind === "SLOPE_LINE") {
            ctx.strokeStyle = meta.color;
            ctx.lineWidth = isHighlight ? 2.4 : 1.6;
            ctx.globalAlpha = feature.status === "BROKEN" ? 0.45 : isHighlight ? 1 : 0.88;
            ctx.setLineDash(feature.status === "ACTIVE" ? [] : [4, 4]);
            const endPt = strokeSlopeGeometry(ctx, g, args.coords, args.scale, args.bounds);
            ctx.setLineDash([]);
            ctx.fillStyle = meta.color;
            for (const anchor of [g.anchor_start, g.anchor_end]) {
              const ax = args.coords.timeToX(anchor.ts);
              const ay = args.coords.priceToY(Number(anchor.price), args.scale, args.bounds);
              if (Number.isFinite(ax) && Number.isFinite(ay)) {
                ctx.beginPath();
                ctx.arc(ax, ay, isHighlight ? 4 : 3, 0, Math.PI * 2);
                ctx.fill();
              }
            }
            ctx.globalAlpha = 1;
            if (endPt && (isHighlight || feature.strength >= 0.65)) {
              drawPill(ctx, label, endPt.endX - 140, endPt.endY, meta.color, "#0b1016", width);
            }
          } else if (g.kind === "CHANNEL") {
            const x1 = args.coords.timeToX(Math.min(g.upper.anchor_start.ts, g.lower.anchor_start.ts));
            const x2Raw = args.coords.timeToX(Math.max(g.upper.anchor_end.ts, g.lower.anchor_end.ts));
            const endTs =
              (g.upper.extend_right || g.lower.extend_right) && x2Raw > x1 && width > x2Raw
                ? Math.max(g.upper.anchor_end.ts, g.lower.anchor_end.ts) +
                  ((Math.max(g.upper.anchor_end.ts, g.lower.anchor_end.ts) -
                    Math.min(g.upper.anchor_start.ts, g.lower.anchor_start.ts)) /
                    (x2Raw - x1)) *
                    (width - x2Raw)
                : Math.max(g.upper.anchor_end.ts, g.lower.anchor_end.ts);
            const minStartTs = Math.min(g.upper.anchor_start.ts, g.lower.anchor_start.ts);
            const uS = slopePriceAtTs(g.upper.anchor_start, g.upper.anchor_end, g.upper.scale, minStartTs);
            const uE = slopePriceAtTs(g.upper.anchor_start, g.upper.anchor_end, g.upper.scale, endTs);
            const lS = slopePriceAtTs(g.lower.anchor_start, g.lower.anchor_end, g.lower.scale, minStartTs);
            const lE = slopePriceAtTs(g.lower.anchor_start, g.lower.anchor_end, g.lower.scale, endTs);
            if (uS !== null && uE !== null && lS !== null && lE !== null) {
              const xE = args.coords.timeToX(endTs);
              ctx.fillStyle = meta.color;
              ctx.globalAlpha = isHighlight ? 0.2 : 0.1;
              ctx.beginPath();
              ctx.moveTo(x1, args.coords.priceToY(uS, args.scale, args.bounds));
              ctx.lineTo(xE, args.coords.priceToY(uE, args.scale, args.bounds));
              ctx.lineTo(xE, args.coords.priceToY(lE, args.scale, args.bounds));
              ctx.lineTo(x1, args.coords.priceToY(lS, args.scale, args.bounds));
              ctx.closePath();
              ctx.fill();
            }
            ctx.strokeStyle = meta.color;
            ctx.lineWidth = isHighlight ? 2 : 1.4;
            ctx.globalAlpha = isHighlight ? 0.95 : 0.8;
            strokeSlopeGeometry(ctx, g.upper, args.coords, args.scale, args.bounds);
            strokeSlopeGeometry(ctx, g.lower, args.coords, args.scale, args.bounds);
            ctx.globalAlpha = 1;
            if (isHighlight) {
              drawPill(ctx, label, width - 160, (topBound + bottomBound) * 0.25, meta.color, "#0b1016", width);
            }
          }

          // Truth in Analysis: render analysis_window vertical sample bounds on hover/highlight.
          if (isHighlight && feature.analysis_window) {
            const winStartX = args.coords.timeToX(feature.analysis_window.from_ts);
            const winEndX = args.coords.timeToX(feature.analysis_window.to_ts);
            ctx.save();
            ctx.strokeStyle = meta.color;
            ctx.lineWidth = 1;
            ctx.globalAlpha = 0.55;
            ctx.setLineDash([2, 3]);
            for (const wx of [winStartX, winEndX]) {
              if (Number.isFinite(wx) && wx >= 0 && wx <= width) {
                ctx.beginPath();
                ctx.moveTo(wx, topBound);
                ctx.lineTo(wx, bottomBound);
                ctx.stroke();
              }
            }
            ctx.setLineDash([]);
            ctx.globalAlpha = 0.95;
            const info = `${feature.detector_id}${
              feature.source_timeframe ? ` · ${feature.source_timeframe}` : ""
            } · Str ${(feature.strength * 100).toFixed(0)}%`;
            drawPill(
              ctx,
              info,
              Number.isFinite(winEndX) ? winEndX - 150 : 12,
              topBound + 18,
              "#151b26",
              "#e8eef8",
              width,
            );
            ctx.restore();
          }
        }
      });
    },
    destroy() {
      if (lastChartId) latestProjectionByChart.delete(lastChartId);
      canvas = null;
    },
  };
}

function createSystemLevelLayer(): RendererLayerInstance {
  let canvas: HTMLCanvasElement | null = null;
  let lastChartId: string | null = null;

  return {
    mount(el) {
      canvas = el;
    },
    render(args) {
      const payload = args.data as (SystemLevelLayerData & { chartId?: string }) | undefined;
      if (payload?.chartId) {
        lastChartId = payload.chartId;
        latestProjectionByChart.set(payload.chartId, {
          coords: args.coords,
          scale: args.scale,
          bounds: args.bounds,
        });
      }
      withCanvas2d(canvas, args, (ctx) => {
        if (!payload || payload.levels.length === 0) return;
        const hovered =
          args.cursor !== null
            ? hitTestSystemLevel(
                payload.levels,
                args.cursor.y,
                args.coords,
                args.scale,
                args.bounds,
              )
            : null;
        const width = args.coords.width;
        for (const level of payload.levels) {
          const meta = categoryMeta(level.category);
          const isHighlight =
            hovered?.level_id === level.level_id ||
            payload.highlightId === level.level_id;
          const isEstimated = (level.evidence_class ?? "OBSERVED") === "ESTIMATED";
          const label = levelLabel(level, payload.precision);

          if (isZoneLevel(level)) {
            const yTop = args.coords.priceToY(Number(level.price_high), args.scale, args.bounds);
            const yBottom = args.coords.priceToY(Number(level.price_low), args.scale, args.bounds);
            const top = Math.min(yTop, yBottom);
            const h = Math.max(3, Math.abs(yBottom - yTop));
            ctx.fillStyle = meta.color;
            ctx.globalAlpha = isHighlight ? 0.24 : 0.12;
            ctx.fillRect(0, top, width, h);
            ctx.globalAlpha = isHighlight ? 0.95 : 0.65;
            ctx.strokeStyle = meta.color;
            ctx.lineWidth = isHighlight ? 1.5 : 1;
            ctx.setLineDash(isEstimated ? [4, 4] : []);
            ctx.strokeRect(0, top, width, h);
            ctx.setLineDash([]);
            ctx.globalAlpha = 1;
            drawPill(ctx, label, width, top + h / 2, meta.color, "#0b1016", width);
          } else {
            const y = args.coords.priceToY(levelPrice(level), args.scale, args.bounds);
            if (!Number.isFinite(y)) continue;
            ctx.strokeStyle = meta.color;
            ctx.lineWidth = isHighlight ? 2 : 1.1;
            ctx.globalAlpha = level.status === "BROKEN" ? 0.45 : isHighlight ? 1 : 0.78;
            ctx.setLineDash(
              isEstimated ? [3, 4] : level.status === "ACTIVE" ? [6, 4] : [2, 3],
            );
            ctx.beginPath();
            ctx.moveTo(0, y);
            ctx.lineTo(width, y);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.globalAlpha = 1;
            if (isHighlight || level.strength >= 0.6) {
              drawPill(ctx, label, 10, y, meta.color, "#0b1016", width);
            }
          }
        }
      });
    },
    destroy() {
      if (lastChartId) latestProjectionByChart.delete(lastChartId);
      canvas = null;
    },
  };
}

function createAgentAnnotationLayer(): RendererLayerInstance {
  let canvas: HTMLCanvasElement | null = null;

  return {
    mount(el) {
      canvas = el;
    },
    render(args) {
      const payload = args.data as (AgentAnnotationLayerData & { chartId?: string }) | undefined;
      if (payload?.chartId) {
        latestProjectionByChart.set(payload.chartId, {
          coords: args.coords,
          scale: args.scale,
          bounds: args.bounds,
        });
      }
      withCanvas2d(canvas, args, (ctx) => {
        if (!payload || payload.annotations.length === 0) return;
        const hovered =
          args.cursor !== null
            ? hitTestAgentAnnotation(
                payload.annotations,
                args.cursor.x,
                args.cursor.y,
                payload.timeframe,
                args.coords,
                args.scale,
                args.bounds,
              )
            : null;
        const tfMs = timeframeMs(payload.timeframe);
        for (const annotation of payload.annotations) {
          if (annotation.status !== "ACTIVE") continue;
          const isHighlight =
            hovered?.annotation_id === annotation.annotation_id ||
            payload.highlightId === annotation.annotation_id;
          const startX = args.coords.timeToX(annotation.ts_anchor);
          const color = "#22d3ee";
          if (annotation.kind === "ZONE") {
            const endX = args.coords.timeToX(annotationZoneEnd(annotation, tfMs));
            const yTop = args.coords.priceToY(Number(annotation.price_high), args.scale, args.bounds);
            const yBottom = args.coords.priceToY(Number(annotation.price_low), args.scale, args.bounds);
            const x = Math.min(startX, endX);
            const w = Math.max(12, Math.abs(endX - startX));
            const y = Math.min(yTop, yBottom);
            const h = Math.max(6, Math.abs(yBottom - yTop));
            ctx.fillStyle = color;
            ctx.globalAlpha = isHighlight ? 0.25 : 0.14;
            ctx.fillRect(x, y, w, h);
            ctx.globalAlpha = isHighlight ? 1 : 0.8;
            ctx.strokeStyle = color;
            ctx.lineWidth = isHighlight ? 2 : 1.2;
            ctx.setLineDash([5, 3]);
            ctx.strokeRect(x, y, w, h);
            ctx.setLineDash([]);
            ctx.globalAlpha = 1;
            drawPill(ctx, annotationSummary(annotation), x + 4, y + 10, "#083344", "#cffafe", args.coords.width);
          } else {
            const y = args.coords.priceToY(Number(annotation.price_low), args.scale, args.bounds);
            if (!Number.isFinite(y)) continue;
            ctx.strokeStyle = color;
            ctx.lineWidth = isHighlight ? 2 : 1.4;
            ctx.setLineDash([4, 3]);
            ctx.beginPath();
            ctx.moveTo(Math.max(0, startX), y);
            ctx.lineTo(args.coords.width, y);
            ctx.stroke();
            ctx.setLineDash([]);
            drawPill(
              ctx,
              annotationSummary(annotation),
              Math.max(8, startX + 4),
              y,
              "#083344",
              "#cffafe",
              args.coords.width,
            );
          }
        }
      });
    },
    destroy() {
      canvas = null;
    },
  };
}

function formatSignedPnl(raw: string): string {
  const num = Number(raw);
  if (!Number.isFinite(num)) return raw;
  const sign = num > 0 ? "+" : "";
  return `${sign}${num.toFixed(2)}`;
}

function compactAccountLabel(accountId: string, maxLen = 18): string {
  if (accountId.length <= maxLen) return accountId;
  return `${accountId.slice(0, maxLen - 1)}…`;
}

function createExecutionBarrierLayer(): RendererLayerInstance {
  let canvas: HTMLCanvasElement | null = null;
  return {
    mount(el) {
      canvas = el;
    },
    render(args) {
      const payload = args.data as (ExecutionBarrierLayerData & { chartId?: string }) | undefined;
      if (payload?.chartId) {
        latestProjectionByChart.set(payload.chartId, {
          coords: args.coords,
          scale: args.scale,
          bounds: args.bounds,
        });
      }
      withCanvas2d(canvas, args, (ctx) => {
        if (!payload || payload.barriers.length === 0) return;
        for (const item of payload.barriers) {
          const acct = compactAccountLabel(item.sub_account_id);
          // 1. Pending entry band (entry_low - entry_high)
          if (item.entry_low != null && item.entry_high != null) {
            const lowNum = Number(item.entry_low);
            const highNum = Number(item.entry_high);
            if (Number.isFinite(lowNum) && Number.isFinite(highNum)) {
              const yHigh = args.coords.priceToY(highNum, args.scale, args.bounds);
              const yLow = args.coords.priceToY(lowNum, args.scale, args.bounds);
              if (Number.isFinite(yHigh) && Number.isFinite(yLow)) {
                const top = Math.min(yHigh, yLow);
                const height = Math.max(2, Math.abs(yLow - yHigh));
                const isLong = item.direction === "LONG";
                ctx.fillStyle = isLong ? "rgba(59, 130, 246, 0.14)" : "rgba(245, 158, 11, 0.14)";
                ctx.fillRect(0, top, args.coords.width, height);
                ctx.strokeStyle = isLong ? "#3b82f6" : "#f59e0b";
                ctx.lineWidth = 1.2;
                ctx.setLineDash([5, 3]);
                ctx.beginPath();
                ctx.moveTo(0, top);
                ctx.lineTo(args.coords.width, top);
                ctx.moveTo(0, top + height);
                ctx.lineTo(args.coords.width, top + height);
                ctx.stroke();
                ctx.setLineDash([]);
                drawPill(
                  ctx,
                  `${acct} · ENTRY ${item.entry_low}–${item.entry_high}`,
                  12,
                  top + height / 2,
                  isLong ? "#1e3a8a" : "#78350f",
                  isLong ? "#dbeafe" : "#fef3c7",
                  args.coords.width,
                );
              }
            }
          }

          // 2. Entry price line + floating PnL badge for open positions
          const entryNum = item.entry_price != null ? Number(item.entry_price) : Number.NaN;
          const pnlNum = item.unrealized_pnl != null ? Number(item.unrealized_pnl) : Number.NaN;
          if (Number.isFinite(entryNum)) {
            const entryY = args.coords.priceToY(entryNum, args.scale, args.bounds);
            if (Number.isFinite(entryY)) {
              ctx.strokeStyle = item.direction === "LONG" ? "#60a5fa" : "#fbbf24";
              ctx.lineWidth = 1.2;
              ctx.setLineDash([2, 3]);
              ctx.beginPath();
              ctx.moveTo(0, entryY);
              ctx.lineTo(args.coords.width, entryY);
              ctx.stroke();
              ctx.setLineDash([]);
              const hasPnl = Number.isFinite(pnlNum) && item.unrealized_pnl != null;
              const label = hasPnl
                ? `${acct} · ${item.direction} @ ${item.entry_price} · PnL ${formatSignedPnl(item.unrealized_pnl!)}`
                : `${acct} · ${item.direction} @ ${item.entry_price}`;
              const bg = !hasPnl ? "#1e293b" : pnlNum >= 0 ? "#064e3b" : "#7f1d1d";
              const fg = !hasPnl ? "#e2e8f0" : pnlNum >= 0 ? "#d1fae5" : "#fee2e2";
              drawPill(ctx, label, 12, entryY, bg, fg, args.coords.width);
            }
          } else if (Number.isFinite(pnlNum) && item.unrealized_pnl != null && item.mark_price != null) {
            const markY = args.coords.priceToY(Number(item.mark_price), args.scale, args.bounds);
            if (Number.isFinite(markY)) {
              drawPill(
                ctx,
                `${acct} · PnL ${formatSignedPnl(item.unrealized_pnl)}`,
                12,
                markY,
                pnlNum >= 0 ? "#064e3b" : "#7f1d1d",
                pnlNum >= 0 ? "#d1fae5" : "#fee2e2",
                args.coords.width,
              );
            }
          }

          // 3 & 4. Stop loss and take profit barrier lines are laid out via the shared
          // `layoutBarrierTargets` helper below so multi-sub-account pills stagger horizontally
          // and stay 100% pixel-aligned with `BarrierDragController` hit-testing.
        }

        ctx.font = "600 10px system-ui, -apple-system, sans-serif";
        const targets = layoutBarrierTargets(
          payload.barriers,
          (p) => args.coords.priceToY(p, args.scale, args.bounds),
          args.coords.width,
          {
            measureTextWidth: (text) => ctx.measureText(text).width,
            readOnly: payload.readOnly,
            replayActive: payload.replayActive,
          },
        );
        const overlay = payload.dragOverlay ?? null;
        const hover = payload.hover ?? null;

        for (const target of targets) {
          const isOverlayTarget =
            overlay !== null &&
            overlay.roundId === target.barrier.round_id &&
            overlay.subAccountId === target.barrier.sub_account_id &&
            overlay.kind === target.kind &&
            overlay.tpIndex === target.tpIndex;
          const isHovered =
            hover !== null &&
            hover.roundId === target.barrier.round_id &&
            hover.subAccountId === target.barrier.sub_account_id &&
            hover.kind === target.kind &&
            hover.tpIndex === target.tpIndex;

          const baseStroke = target.kind === "STOP" ? "#ef4444" : "#10b981";
          const pillBg = target.kind === "STOP" ? "#7f1d1d" : "#064e3b";
          const pillFg = target.kind === "STOP" ? "#fee2e2" : "#d1fae5";

          ctx.save();
          if (isOverlayTarget) {
            ctx.globalAlpha = 0.4;
          } else if (!target.draggable) {
            ctx.globalAlpha = 0.65;
          }

          ctx.strokeStyle = baseStroke;
          ctx.lineWidth = isHovered && target.draggable ? 2.2 : 1.4;
          ctx.setLineDash([4, 4]);
          ctx.beginPath();
          ctx.moveTo(0, target.lineY);
          ctx.lineTo(args.coords.width, target.lineY);
          ctx.stroke();
          ctx.setLineDash([]);

          const pillLabel = target.draggable
            ? target.label
            : `${target.label} [🔒]`;
          drawPill(
            ctx,
            pillLabel,
            target.pillRect.x,
            target.lineY,
            pillBg,
            pillFg,
            args.coords.width,
          );

          if (isHovered && target.draggable && !isOverlayTarget) {
            ctx.strokeStyle = "#f8fafc";
            ctx.lineWidth = 1.25;
            ctx.beginPath();
            ctx.roundRect(
              target.pillRect.x - 1,
              target.pillRect.y - 1,
              target.pillRect.w + 2,
              target.pillRect.h + 2,
              5,
            );
            ctx.stroke();
          }
          ctx.restore();
        }

        // 5. Active drag / pending_confirm / pending_ack preview overlay
        if (overlay) {
          const previewNum = Number(overlay.previewPrice);
          const previewY = Number.isFinite(previewNum)
            ? args.coords.priceToY(previewNum, args.scale, args.bounds)
            : Number.NaN;
          if (Number.isFinite(previewY)) {
            let stroke = "#10b981";
            let bg = "#064e3b";
            let fg = "#d1fae5";
            let dash: number[] = [];

            if (overlay.phase === "pending_ack") {
              stroke = "#f59e0b";
              bg = "#78350f";
              fg = "#fef3c7";
              dash = [6, 4];
            } else if (!overlay.classification.isValidGeometry) {
              stroke = "#ef4444";
              bg = "#7f1d1d";
              fg = "#fee2e2";
              dash = [3, 3];
            } else if (overlay.classification.requiresRiskCheck) {
              stroke = "#f59e0b";
              bg = "#78350f";
              fg = "#fef3c7";
            } else if (overlay.classification.crossesBreakeven) {
              stroke = "#38bdf8";
              bg = "#0c4a6e";
              fg = "#e0f2fe";
            }

            ctx.save();
            ctx.strokeStyle = stroke;
            ctx.lineWidth = 2;
            ctx.setLineDash(dash);
            ctx.beginPath();
            ctx.moveTo(0, previewY);
            ctx.lineTo(args.coords.width, previewY);
            ctx.stroke();
            ctx.setLineDash([]);
            drawPill(
              ctx,
              overlay.badgeText,
              12,
              previewY,
              bg,
              fg,
              args.coords.width,
            );
            ctx.restore();
          }
        }
      });
    },
    destroy() {
      canvas = null;
    },
  };
}

function createStagedDraftLayer(): RendererLayerInstance {
  let canvas: HTMLCanvasElement | null = null;
  return {
    mount(el) {
      canvas = el;
    },
    render(args) {
      const payload = args.data as (StagedDraftLayerData & { chartId?: string }) | undefined;
      if (payload?.chartId) {
        latestProjectionByChart.set(payload.chartId, {
          coords: args.coords,
          scale: args.scale,
          bounds: args.bounds,
        });
      }
      withCanvas2d(canvas, args, (ctx) => {
        if (!payload || payload.drafts.length === 0) return;
        const activeDrafts = payload.drafts.filter(isNonTerminalDraft);
        if (activeDrafts.length === 0) return;
        const hovered =
          args.cursor !== null
            ? hitTestStagedDraft(
                activeDrafts,
                args.cursor.y,
                args.coords,
                args.scale,
                args.bounds,
              )
            : null;
        const width = args.coords.width;

        for (const draft of activeDrafts) {
          const isHighlight =
            hovered?.draft_id === draft.draft_id || payload.highlightId === draft.draft_id;
          const { content } = draft;
          const isConfirmed = draft.status === "CONFIRMED";
          const entryColor = content.direction === "LONG" ? "#f59e0b" : "#a855f7";
          const entryBg = content.direction === "LONG" ? "#78350f" : "#4c1d95";
          const entryFg = content.direction === "LONG" ? "#fef3c7" : "#f3e8ff";

          const yHigh = args.coords.priceToY(Number(content.entry_high), args.scale, args.bounds);
          const yLow = args.coords.priceToY(Number(content.entry_low), args.scale, args.bounds);
          if (Number.isFinite(yHigh) && Number.isFinite(yLow)) {
            const top = Math.min(yHigh, yLow);
            const h = Math.max(4, Math.abs(yLow - yHigh));
            ctx.fillStyle = entryColor;
            ctx.globalAlpha = isHighlight ? 0.2 : 0.1;
            ctx.fillRect(0, top, width, h);
            ctx.globalAlpha = isHighlight ? 0.98 : 0.8;
            ctx.strokeStyle = entryColor;
            ctx.lineWidth = isHighlight ? 2 : 1.25;
            ctx.setLineDash([6, 3]);
            ctx.strokeRect(0, top, width, h);
            ctx.setLineDash([]);
            ctx.globalAlpha = 1;
            const entryText =
              content.entry_low === content.entry_high
                ? content.entry_low
                : `${content.entry_low}–${content.entry_high}`;
            const tag = isConfirmed ? "CONFIRMED" : "DRAFT";
            drawPill(
              ctx,
              `${tag} ${content.direction} · ${content.sub_account_id} · ${entryText}`,
              width,
              top + h / 2,
              entryBg,
              entryFg,
              width,
            );
          }

          const stopY = args.coords.priceToY(
            Number(content.invalidation_stop),
            args.scale,
            args.bounds,
          );
          if (Number.isFinite(stopY)) {
            ctx.strokeStyle = "#fb923c";
            ctx.lineWidth = isHighlight ? 2 : 1.2;
            ctx.setLineDash([3, 3]);
            ctx.beginPath();
            ctx.moveTo(0, stopY);
            ctx.lineTo(width, stopY);
            ctx.stroke();
            ctx.setLineDash([]);
            drawPill(
              ctx,
              `DRAFT SL · ${content.sub_account_id} ${content.invalidation_stop}`,
              width,
              stopY,
              "#7c2d12",
              "#ffedd5",
              width,
            );
          }

          content.take_profit.forEach((tp, idx) => {
            const tpY = args.coords.priceToY(Number(tp), args.scale, args.bounds);
            if (!Number.isFinite(tpY)) return;
            ctx.strokeStyle = "#38bdf8";
            ctx.lineWidth = isHighlight ? 2 : 1.2;
            ctx.setLineDash([3, 3]);
            ctx.beginPath();
            ctx.moveTo(0, tpY);
            ctx.lineTo(width, tpY);
            ctx.stroke();
            ctx.setLineDash([]);
            drawPill(
              ctx,
              `DRAFT TP${idx + 1} · ${content.sub_account_id} ${tp}`,
              width,
              tpY,
              "#0c4a6e",
              "#e0f2fe",
              width,
            );
          });
        }
      });
    },
    destroy() {
      canvas = null;
    },
  };
}

function createLayerChannelIndicator(layerId: string): NativeIndicator {
  let ctx: NativeIndicatorContext | null = null;
  let chartId = "";
  let latestPayload: unknown = null;

  return {
    start(nextCtx, inputs) {
      ctx = nextCtx;
      chartId = String(inputs.chartId ?? "");
      const key = `${chartId}:${layerId}`;
      if (chartId) {
        channelPushers.set(key, (payload) => {
          latestPayload = payload;
          ctx?.pushData(payload);
        });
        if (pendingLayerPayloads.has(key)) {
          latestPayload = pendingLayerPayloads.get(key);
        }
      }
      nextCtx.emit({});
      if (latestPayload !== null && latestPayload !== undefined) {
        nextCtx.pushData(latestPayload);
      }
    },
    onBars() {
      // Layer reads live coords/bars on renderer frame automatically.
    },
    onViewport() {
      // Layer repaints natively on viewport pan/zoom.
    },
    setInputs(inputs) {
      const nextChartId = String(inputs.chartId ?? chartId);
      if (nextChartId !== chartId && chartId) {
        channelPushers.delete(`${chartId}:${layerId}`);
      }
      chartId = nextChartId;
      if (chartId) {
        channelPushers.set(`${chartId}:${layerId}`, (payload) => {
          latestPayload = payload;
          ctx?.pushData(payload);
        });
      }
    },
    suspend() {
      ctx?.pushData(undefined);
    },
    resume() {
      if (latestPayload !== null) ctx?.pushData(latestPayload);
    },
    stop() {
      if (chartId) {
        channelPushers.delete(`${chartId}:${layerId}`);
      }
      ctx?.pushData(undefined);
      ctx = null;
    },
  };
}

/**
 * Register the 3 read-only native renderer layers (`SYSTEM_LEVEL`, `AGENT_ANNOTATION`,
 * `EXECUTION_BARRIER`) and their legend-less native data channel hooks.
 */
export function ensureTalariaLayersRegistered(): void {
  if (registered) return;
  registered = true;

  registerRendererLayer({
    id: LAYER_SYSTEM_LEVEL,
    placement: "below-data",
    repaintOnCursor: true,
    create: createSystemLevelLayer,
  });

  registerRendererLayer({
    id: LAYER_SYSTEM_STRUCTURE,
    placement: "below-data",
    repaintOnCursor: true,
    create: createSystemStructureLayer,
  });

  registerRendererLayer({
    id: LAYER_AGENT_ANNOTATION,
    placement: "above-data",
    repaintOnCursor: true,
    create: createAgentAnnotationLayer,
  });

  registerRendererLayer({
    id: LAYER_EXECUTION_BARRIER,
    placement: "above-data",
    repaintOnCursor: true,
    create: createExecutionBarrierLayer,
  });

  registerRendererLayer({
    id: LAYER_STAGED_DRAFT,
    placement: "above-data",
    repaintOnCursor: true,
    create: createStagedDraftLayer,
  });

  for (const layerId of [
    LAYER_SYSTEM_LEVEL,
    LAYER_SYSTEM_STRUCTURE,
    LAYER_AGENT_ANNOTATION,
    LAYER_EXECUTION_BARRIER,
    LAYER_STAGED_DRAFT,
  ]) {
    registerNativeIndicator({
      type: layerId,
      title: layerId,
      paneHint: "price",
      overlay: true,
      legend: false,
      multiInstance: false,
      inputsSchema: () => [{ key: "chartId", title: "Chart ID", type: "string", defval: "" }],
      defaultInputs: () => ({ chartId: "" }),
      create: () => createLayerChannelIndicator(layerId),
    });
  }
}
