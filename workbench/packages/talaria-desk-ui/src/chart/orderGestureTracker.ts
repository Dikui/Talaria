import type { SerializedDrawing, Vela } from "@luxalgo/vela";

import { getLatestProjection, type ProjectedCoordinateContext } from "./velaLayers";

export type OrderGesturePhase = "placing" | "dragging" | "placed";

export interface OrderGestureAnchorPx {
  x: number;
  y: number;
  topY: number;
  bottomY: number;
  leftX: number;
  rightX: number;
}

export interface OrderGestureGeometry {
  drawingId: string | null;
  phase: OrderGesturePhase;
  direction: "LONG" | "SHORT";
  entry: number;
  stop: number;
  target: number;
  entryStr: string;
  stopStr: string;
  targetStr: string;
  rr: number;
  validGeometry: boolean;
  anchorPx: OrderGestureAnchorPx | null;
}

/** Vela PositionTool.DEFAULT_RR = 2 (chunk-SS7AT5QJ.js:5262). */
const VELA_POSITION_DEFAULT_RR = 2;
const HANDLE_HIT_RADIUS_PX = 12;

export function formatGesturePrice(price: number, precision: number): string {
  const digits = Math.min(8, Math.max(0, Math.trunc(precision)));
  if (!Number.isFinite(price)) return "0";
  return price.toFixed(digits);
}

/**
 * Validates strict price separation after rounding to `pricePrecision`, matching
 * `DraftContent._geometry` (`core/src/talaria/foundation/paper.py:57-65`):
 * - LONG: stop < entry_low and target > entry_high (here entry_low == entry_high)
 * - SHORT: stop > entry_high and target < entry_low
 */
export function isValidOrderGeometry(
  direction: "LONG" | "SHORT",
  entryStr: string,
  stopStr: string,
  targetStr: string,
): boolean {
  const entry = Number(entryStr);
  const stop = Number(stopStr);
  const target = Number(targetStr);
  if (
    !Number.isFinite(entry) ||
    !Number.isFinite(stop) ||
    !Number.isFinite(target) ||
    entry <= 0 ||
    stop <= 0 ||
    target <= 0
  ) {
    return false;
  }
  if (direction === "LONG") {
    return stop < entry && target > entry;
  }
  return stop > entry && target < entry;
}

function computeAnchorPx(
  anchors: ReadonlyArray<{ time: number; price: number }>,
  entry: number,
  stop: number,
  target: number,
  proj: ProjectedCoordinateContext | null | undefined,
): OrderGestureAnchorPx | null {
  if (!proj) return null;
  const ey = proj.coords.priceToY(entry, proj.scale, proj.bounds);
  const sy = proj.coords.priceToY(stop, proj.scale, proj.bounds);
  const ty = proj.coords.priceToY(target, proj.scale, proj.bounds);
  if (!Number.isFinite(ey) || !Number.isFinite(sy) || !Number.isFinite(ty)) return null;
  const xs = anchors
    .map((a) => proj.coords.timeToX(a.time))
    .filter((v) => Number.isFinite(v));
  const leftX = xs.length > 0 ? Math.min(...xs) : proj.coords.width * 0.5;
  const rightX = xs.length > 0 ? Math.max(...xs) : leftX;
  const topY = Math.min(ey, sy, ty);
  const bottomY = Math.max(ey, sy, ty);
  return {
    x: rightX,
    y: ey,
    topY,
    bottomY,
    leftX,
    rightX,
  };
}

/**
 * Extracts normalized order gesture geometry from either:
 * - a 2-anchor placement ghost (`drawing:draft`), deriving stop via DEFAULT_RR = 2; or
 * - a 3-anchor placed `position` drawing (`[entry, stop, target]`).
 */
export function extractPositionGeometry(
  doc: SerializedDrawing,
  precision: number,
  phase: OrderGesturePhase,
  proj?: ProjectedCoordinateContext | null,
): OrderGestureGeometry | null {
  if (doc.type !== "position" || doc.anchors.length < 2) return null;
  const entryAnchor = doc.anchors[0];
  let stopPrice: number;
  let targetPrice: number;

  if (doc.anchors.length >= 3) {
    stopPrice = doc.anchors[1].price;
    targetPrice = doc.anchors[2].price;
  } else {
    targetPrice = doc.anchors[1].price;
    stopPrice = entryAnchor.price - (targetPrice - entryAnchor.price) / VELA_POSITION_DEFAULT_RR;
  }

  const entry = entryAnchor.price;
  const direction: "LONG" | "SHORT" = targetPrice >= entry ? "LONG" : "SHORT";
  const entryStr = formatGesturePrice(entry, precision);
  const stopStr = formatGesturePrice(stopPrice, precision);
  const targetStr = formatGesturePrice(targetPrice, precision);
  const risk = Math.abs(entry - stopPrice);
  const rr = risk > 1e-9 ? Math.abs(targetPrice - entry) / risk : 0;

  return {
    drawingId: phase === "placing" ? null : doc.id,
    phase,
    direction,
    entry,
    stop: stopPrice,
    target: targetPrice,
    entryStr,
    stopStr,
    targetStr,
    rr,
    validGeometry: isValidOrderGeometry(direction, entryStr, stopStr, targetStr),
    anchorPx: computeAnchorPx(doc.anchors, entry, stopPrice, targetPrice, proj),
  };
}

/**
 * Exact mirror of Vela 0.10.17 `PositionTool.constrainHandleDrag(index)` and
 * `Drawing.translateBody(dt, dp, orig)` (`chunk-SS7AT5QJ.js:177-179, 5055-5065`).
 * Used by the passive Shadow Handle-Drag Tracker because Vela emits zero
 * `drawing:draft`/`drawing:edited` events while dragging an existing drawing's handles.
 */
export function simulatePositionHandleDrag(
  origAnchors: ReadonlyArray<{ time: number; price: number }>,
  handleIndex: number,
  cursorPrice: number,
  cursorTime: number,
  startPrice: number,
  startTime: number,
): Array<{ time: number; price: number }> {
  const next = origAnchors.map((a) => ({ time: a.time, price: a.price }));
  if (next.length < 3) return next;

  if (handleIndex < 0) {
    const dt = cursorTime - startTime;
    const dp = cursorPrice - startPrice;
    for (const a of next) {
      a.time += dt;
      a.price += dp;
    }
    return next;
  }

  if (handleIndex >= 0 && handleIndex < next.length) {
    next[handleIndex] = { time: cursorTime, price: cursorPrice };
    const [entry, stop, target] = next;
    const stopSide = Math.sign(stop.price - entry.price);
    const targetSide = Math.sign(target.price - entry.price);
    if (stopSide !== 0 && targetSide !== 0 && stopSide === targetSide) {
      if (handleIndex === 2) {
        stop.price = 2 * entry.price - stop.price;
      } else {
        target.price = 2 * entry.price - target.price;
      }
    }
  }
  return next;
}

export interface OrderGestureTrackerOptions {
  chart: Vela;
  container: HTMLElement;
  chartId: string;
  getPricePrecision: () => number;
  onGestureChange?: (gesture: OrderGestureGeometry | null) => void;
}

/**
 * Passive capture-phase tracker for Vela's built-in `'position'` drawing.
 *
 * - Placement phase (`placing`): consumes `drawing:draft` per cursor frame.
 * - Placed handle/body drag phase (`dragging`): observes `pointerdown`/`pointermove`
 *   in capture phase WITHOUT calling `stopPropagation()` or `preventDefault()`,
 *   recomputing live constrained `{ entry, stop, target, direction }` per frame (<0.1ms)
 *   for <5ms Rust sizing preview, then reconciles with `drawing:edited` on `pointerup`.
 * - Enforces at most 1 active ephemeral `'position'` per chart and applies optional
 *   `preferredDirection` ("SHORT") when a bare click falls back to Vela's default LONG box.
 */
export class OrderGestureTracker {
  private readonly chart: Vela;
  private readonly container: HTMLElement;
  private readonly chartId: string;
  private readonly getPricePrecision: () => number;
  private readonly onGestureChange?: (gesture: OrderGestureGeometry | null) => void;

  private activePositionId: string | null = null;
  private preferredDirection: "LONG" | "SHORT" | null = null;
  private shadowDrag: {
    drawingId: string;
    origAnchors: Array<{ time: number; price: number }>;
    handleIndex: number;
    startPrice: number;
    startTime: number;
    targetEl: HTMLCanvasElement;
  } | null = null;
  private unsubscribers: Array<() => void> = [];

  constructor(options: OrderGestureTrackerOptions) {
    this.chart = options.chart;
    this.container = options.container;
    this.chartId = options.chartId;
    this.getPricePrecision = options.getPricePrecision;
    this.onGestureChange = options.onGestureChange;

    this.attach();
  }

  armOrderTool(direction: "LONG" | "SHORT" | null): void {
    this.preferredDirection = direction;
    this.chart.drawings.setTool(direction ? "position" : null);
  }

  getActivePositionId(): string | null {
    return this.activePositionId;
  }

  getPreferredDirection(): "LONG" | "SHORT" | null {
    return this.preferredDirection;
  }

  clear(): void {
    this.shadowDrag = null;
    this.preferredDirection = null;
    this.activePositionId = null;
    this.onGestureChange?.(null);
  }

  private emitFromDoc(doc: SerializedDrawing, phase: OrderGesturePhase): void {
    const proj = getLatestProjection(this.chartId);
    const geometry = extractPositionGeometry(doc, this.getPricePrecision(), phase, proj);
    this.onGestureChange?.(geometry);
  }

  private attach(): void {
    this.unsubscribers.push(
      this.chart.on("drawing:draft", ({ doc }) => {
        if (!doc) {
          if (this.activePositionId) {
            const current = this.chart.drawings
              .all()
              .find((d) => d.id === this.activePositionId);
            if (current && current.type === "position") {
              this.emitFromDoc(current, "placed");
              return;
            }
          }
          this.onGestureChange?.(null);
          return;
        }
        if (doc.type !== "position") return;
        this.emitFromDoc(doc, "placing");
      }),

      this.chart.on("drawing:created", ({ id }) => {
        const created = this.chart.drawings.all().find((d) => d.id === id);
        if (!created || created.type !== "position") return;

        // Enforce at most 1 active ephemeral 'position' drawing on this chart.
        const previousPositions = this.chart.drawings
          .all()
          .filter((d) => d.type === "position" && d.id !== id);
        if (previousPositions.length > 0) {
          this.chart.drawings.removeMany(previousPositions.map((d) => d.id));
        }

        let currentDoc = created;
        const preferred = this.preferredDirection;
        this.preferredDirection = null;

        // If user explicitly armed SHORT and bare-clicked (Vela falls back to LONG), mirror across entry.
        if (preferred === "SHORT" && currentDoc.anchors.length >= 3) {
          const [entry, stop, target] = currentDoc.anchors;
          if (target.price > entry.price) {
            const flippedAnchors = [
              { ...entry },
              { time: stop.time, price: 2 * entry.price - stop.price },
              { time: target.time, price: 2 * entry.price - target.price },
            ];
            this.chart.drawings.update(id, {
              anchors: flippedAnchors,
              props: { ...(currentDoc.props ?? {}), showLossSize: false },
            });
            currentDoc = this.chart.drawings.all().find((d) => d.id === id) ?? {
              ...currentDoc,
              anchors: flippedAnchors,
            };
          }
        }

        // Ensure Vela's naive Loss/Size label stays hidden (ADR-0010 / D4).
        if (currentDoc.props?.showLossSize !== false) {
          this.chart.drawings.update(id, {
            props: { ...(currentDoc.props ?? {}), showLossSize: false },
          });
          currentDoc = this.chart.drawings.all().find((d) => d.id === id) ?? currentDoc;
        }

        this.activePositionId = id;
        this.emitFromDoc(currentDoc, "placed");
      }),

      this.chart.on("drawing:edited", ({ id }) => {
        const edited = this.chart.drawings.all().find((d) => d.id === id);
        if (!edited || edited.type !== "position") return;
        this.activePositionId = id;
        this.shadowDrag = null;
        this.emitFromDoc(edited, "placed");
      }),

      this.chart.on("drawing:removed", ({ id }) => {
        if (id === this.activePositionId) {
          this.activePositionId = null;
          this.shadowDrag = null;
          this.onGestureChange?.(null);
        }
      }),

      this.chart.on("drawing:replaced", () => {
        const surviving = this.chart.drawings.all().find((d) => d.type === "position") ?? null;
        this.shadowDrag = null;
        if (surviving) {
          this.activePositionId = surviving.id;
          this.emitFromDoc(surviving, "placed");
        } else if (this.activePositionId !== null) {
          this.activePositionId = null;
          this.onGestureChange?.(null);
        }
      }),

      this.chart.on("drawing:history", () => {
        const surviving = this.chart.drawings.all().find((d) => d.type === "position") ?? null;
        this.shadowDrag = null;
        if (!surviving && this.activePositionId !== null) {
          this.activePositionId = null;
          this.onGestureChange?.(null);
        }
      }),

      this.chart.on("drawing:tool", ({ type }) => {
        if (type !== "position") {
          this.preferredDirection = null;
        }
      }),
    );

    // Passive capture-phase Shadow Handle-Drag Tracker (never stops propagation).
    const onPointerDown = (e: PointerEvent) => {
      if (e.button !== 0 || !(e.target instanceof HTMLCanvasElement)) return;
      if (this.chart.drawings.getTool() !== null || this.chart.drawings.getMode() !== null) return;
      const activeId = this.activePositionId;
      if (!activeId) return;
      const doc = this.chart.drawings.all().find((d) => d.id === activeId);
      if (!doc || doc.type !== "position" || doc.locked || doc.anchors.length < 3) return;

      const proj = getLatestProjection(this.chartId);
      if (!proj) return;
      const rect = e.target.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;

      let hitHandle = -1;
      for (let i = 0; i < 3; i += 1) {
        const hx = proj.coords.timeToX(doc.anchors[i].time);
        const hy = proj.coords.priceToY(doc.anchors[i].price, proj.scale, proj.bounds);
        if (Number.isFinite(hx) && Number.isFinite(hy) && Math.hypot(x - hx, y - hy) <= HANDLE_HIT_RADIUS_PX) {
          hitHandle = i;
          break;
        }
      }

      if (hitHandle < 0) {
        const xs = doc.anchors.map((a) => proj.coords.timeToX(a.time));
        const ys = doc.anchors.map((a) => proj.coords.priceToY(a.price, proj.scale, proj.bounds));
        if (xs.some((v) => !Number.isFinite(v)) || ys.some((v) => !Number.isFinite(v))) return;
        const minX = Math.min(...xs) - 8;
        const maxX = Math.max(...xs) + 8;
        const minY = Math.min(...ys) - 8;
        const maxY = Math.max(...ys) + 8;
        if (x < minX || x > maxX || y < minY || y > maxY) return;
      }

      const startPrice = proj.coords.yToPrice(y, proj.scale, proj.bounds);
      const startTime = proj.coords.logicalToTime(proj.coords.xToLogical(x));
      if (!Number.isFinite(startPrice)) return;

      this.shadowDrag = {
        drawingId: doc.id,
        origAnchors: doc.anchors.map((a) => ({ time: a.time, price: a.price })),
        handleIndex: hitHandle,
        startPrice,
        startTime: Number.isFinite(startTime) ? startTime : doc.anchors[0].time,
        targetEl: e.target,
      };
    };

    const onPointerMove = (e: PointerEvent) => {
      const drag = this.shadowDrag;
      if (!drag) return;
      const proj = getLatestProjection(this.chartId);
      if (!proj) return;
      const rect = drag.targetEl.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const cursorPrice = proj.coords.yToPrice(y, proj.scale, proj.bounds);
      if (!Number.isFinite(cursorPrice)) return;
      const rawTime = proj.coords.logicalToTime(proj.coords.xToLogical(x));
      const cursorTime = Number.isFinite(rawTime) ? rawTime : drag.startTime;

      const simulatedAnchors = simulatePositionHandleDrag(
        drag.origAnchors,
        drag.handleIndex,
        cursorPrice,
        cursorTime,
        drag.startPrice,
        drag.startTime,
      );
      const syntheticDoc: SerializedDrawing = {
        id: drag.drawingId,
        type: "position",
        paneId: "price",
        anchors: simulatedAnchors,
        style: { lineColor: "#4c8dff", lineWidth: 1, lineStyle: "solid" },
        locked: false,
        visible: true,
        zIndex: 0,
        createdAt: 0,
      };
      this.emitFromDoc(syntheticDoc, "dragging");
    };

    const endShadowDrag = () => {
      this.shadowDrag = null;
    };

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && this.shadowDrag) {
        const activeId = this.shadowDrag.drawingId;
        this.shadowDrag = null;
        const doc = this.chart.drawings.all().find((d) => d.id === activeId);
        if (doc && doc.type === "position") {
          this.emitFromDoc(doc, "placed");
        }
      }
    };

    this.container.addEventListener("pointerdown", onPointerDown, { capture: true });
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", endShadowDrag);
    window.addEventListener("pointercancel", endShadowDrag);
    window.addEventListener("keydown", onKeyDown);

    this.unsubscribers.push(() => {
      this.container.removeEventListener("pointerdown", onPointerDown, { capture: true });
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", endShadowDrag);
      window.removeEventListener("pointercancel", endShadowDrag);
      window.removeEventListener("keydown", onKeyDown);
    });
  }

  destroy(): void {
    this.shadowDrag = null;
    this.activePositionId = null;
    this.preferredDirection = null;
    for (const unsub of this.unsubscribers) {
      unsub();
    }
    this.unsubscribers = [];
  }
}
