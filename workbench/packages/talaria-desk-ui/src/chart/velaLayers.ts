import {
  registerNativeIndicator,
  registerRendererLayer,
  type NativeIndicator,
  type NativeIndicatorContext,
  type RendererLayerArgs,
  type RendererLayerInstance,
} from "@luxalgo/vela/plugin";
import type { AgentAnnotationRecord, DraftRecord, KeyLevel } from "@talaria/contracts";

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
import type { ExecutionBarrierItem } from "./adapter";
import {
  layoutBarrierTargets,
  type BarrierDragOverlay,
  type BarrierHoverState,
} from "./barrierDragController";

export const LAYER_SYSTEM_LEVEL = "SYSTEM_LEVEL";
export const LAYER_AGENT_ANNOTATION = "AGENT_ANNOTATION";
export const LAYER_EXECUTION_BARRIER = "EXECUTION_BARRIER";
export const LAYER_STAGED_DRAFT = "STAGED_DRAFT";

export interface SystemLevelLayerData {
  levels: KeyLevel[];
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
