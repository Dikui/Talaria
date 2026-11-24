import {
  registerNativeIndicator,
  registerRendererLayer,
  type NativeIndicator,
  type NativeIndicatorContext,
  type RendererLayerArgs,
  type RendererLayerInstance,
} from "@luxalgo/vela/plugin";
import type { AgentAnnotationRecord, KeyLevel } from "@talaria/contracts";

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

export const LAYER_SYSTEM_LEVEL = "SYSTEM_LEVEL";
export const LAYER_AGENT_ANNOTATION = "AGENT_ANNOTATION";
export const LAYER_EXECUTION_BARRIER = "EXECUTION_BARRIER";

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
}

export interface ProjectedCoordinateContext {
  coords: Pick<LayerCoordinateSystem, "timeToX" | "priceToY" | "width">;
  scale: LayerPriceScale;
  bounds: LayerPaneBounds;
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
  for (const key of [LAYER_SYSTEM_LEVEL, LAYER_AGENT_ANNOTATION, LAYER_EXECUTION_BARRIER]) {
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

function createExecutionBarrierLayer(): RendererLayerInstance {
  let canvas: HTMLCanvasElement | null = null;
  return {
    mount(el) {
      canvas = el;
    },
    render(args) {
      const payload = args.data as ExecutionBarrierLayerData | undefined;
      withCanvas2d(canvas, args, (ctx) => {
        if (!payload || payload.barriers.length === 0) return;
        for (const item of payload.barriers) {
          const stopY = args.coords.priceToY(Number(item.stop), args.scale, args.bounds);
          if (Number.isFinite(stopY)) {
            ctx.strokeStyle = "#ef4444";
            ctx.lineWidth = 1.4;
            ctx.setLineDash([4, 4]);
            ctx.beginPath();
            ctx.moveTo(0, stopY);
            ctx.lineTo(args.coords.width, stopY);
            ctx.stroke();
            ctx.setLineDash([]);
            drawPill(
              ctx,
              `${item.sub_account_id} · SL ${item.stop}`,
              12,
              stopY,
              "#7f1d1d",
              "#fee2e2",
              args.coords.width,
            );
          }
          for (const tp of item.take_profit) {
            const tpY = args.coords.priceToY(Number(tp), args.scale, args.bounds);
            if (Number.isFinite(tpY)) {
              ctx.strokeStyle = "#10b981";
              ctx.lineWidth = 1.4;
              ctx.setLineDash([4, 4]);
              ctx.beginPath();
              ctx.moveTo(0, tpY);
              ctx.lineTo(args.coords.width, tpY);
              ctx.stroke();
              ctx.setLineDash([]);
              drawPill(
                ctx,
                `${item.sub_account_id} · TP ${tp}`,
                12,
                tpY,
                "#064e3b",
                "#d1fae5",
                args.coords.width,
              );
            }
          }
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
    repaintOnCursor: false,
    create: createExecutionBarrierLayer,
  });

  for (const layerId of [
    LAYER_SYSTEM_LEVEL,
    LAYER_AGENT_ANNOTATION,
    LAYER_EXECUTION_BARRIER,
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
