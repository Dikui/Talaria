import type { PaperCommandKind, PaperCommandPayload } from "@talaria/contracts";

import { t } from "../lib/i18n";

export type BarrierKind = "STOP" | "TP";

export type BarrierDragDisabledReason =
  | "OFFLINE"
  | "REPLAY"
  | "HALTED"
  | "READ_ONLY"
  | "NOT_ACTIVE_ROUND";

export interface DraggableBarrierSpec {
  round_id: string;
  sub_account_id: string;
  direction: "LONG" | "SHORT";
  stop: string;
  take_profit: string[];
  phase?: string | null;
  entry_low?: string | null;
  entry_high?: string | null;
  entry_price?: string | null;
  mark_price?: string | null;
  unrealized_pnl?: string | null;
  quantity?: string | null;
  ledger_seq?: number | null;
  draggable?: boolean;
  disabled_reason?: BarrierDragDisabledReason | null;
  account_trading_state?: "ACTIVE" | "REDUCING" | "HALTED" | null;
}

export interface BarrierDragClassification {
  commandKind: Extract<PaperCommandKind, "TIGHTEN_STOP" | "WIDEN_STOP" | "MODIFY_TAKE_PROFIT">;
  isWidening: boolean;
  requiresRiskCheck: boolean;
  crossesBreakeven: boolean;
  isNoop: boolean;
  isValidGeometry: boolean;
  geometryError?: string;
  /** Change in risk expressed in units of the round's current stop risk (R). Negative = tighter risk, positive = wider risk. */
  rDelta: number | null;
  /** Signed PnL / risk delta in quote currency when quantity is known. */
  riskAmountDelta: number | null;
  /** Updated take_profit list when kind === "TP". */
  nextTakeProfits?: string[];
}

export type BarrierOverlayPhase = "dragging" | "pending_confirm" | "pending_ack";

export interface BarrierDragOverlay {
  roundId: string;
  subAccountId: string;
  direction: "LONG" | "SHORT";
  kind: BarrierKind;
  tpIndex: number;
  originalPrice: string;
  previewPrice: string;
  phase: BarrierOverlayPhase;
  badgeText: string;
  classification: BarrierDragClassification;
  commandId?: string;
}

export interface BarrierDragProposal {
  roundId: string;
  subAccountId: string;
  direction: "LONG" | "SHORT";
  kind: BarrierKind;
  tpIndex: number;
  originalPrice: string;
  targetPrice: string;
  entryPrice: string | null;
  basedOnLedgerSeq: number;
  classification: BarrierDragClassification;
  payload: PaperCommandPayload;
}

export interface BarrierPillRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface BarrierHitTarget {
  barrier: DraggableBarrierSpec;
  kind: BarrierKind;
  tpIndex: number;
  price: number;
  priceText: string;
  lineY: number;
  label: string;
  pillRect: BarrierPillRect;
  draggable: boolean;
  disabledReason: BarrierDragDisabledReason | null;
}

export interface BarrierHoverState {
  roundId: string;
  subAccountId: string;
  kind: BarrierKind;
  tpIndex: number;
  draggable: boolean;
  disabledReason: BarrierDragDisabledReason | null;
}

export interface BarrierProjectionContext {
  coords: {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    priceToY: (price: number, scale: any, bounds: any) => number;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    yToPrice: (y: number, scale: any, bounds: any) => number;
    width: number;
    height?: number;
  };
  scale: unknown;
  bounds: { top: number; height: number };
}

export interface BarrierDragDisabledEvent {
  barrier: DraggableBarrierSpec;
  kind: BarrierKind;
  reason: BarrierDragDisabledReason;
  clientX: number;
  clientY: number;
}

export interface BarrierDragControllerOptions {
  container: HTMLElement;
  getProjection: () => BarrierProjectionContext | null;
  getBarriers: () => readonly DraggableBarrierSpec[];
  getPricePrecision: () => number;
  getActiveSubAccountId?: () => string | null;
  isToolActive?: () => boolean;
  isReadOnly?: () => boolean;
  isReplayActive?: () => boolean;
  onOverlayChange: (overlay: BarrierDragOverlay | null) => void;
  onHoverChange?: (hover: BarrierHoverState | null) => void;
  onDragEnd: (proposal: BarrierDragProposal) => void;
  onDisabledAttempt?: (event: BarrierDragDisabledEvent) => void;
  onPendingAckTimeout?: (overlay: BarrierDragOverlay) => void;
  ackTimeoutMs?: number;
}

export const BARRIER_DRAG_SLOP_PX = 3;
export const BARRIER_HIT_TOLERANCE_PX = 8;
export const BARRIER_PILL_STAGGER_PX = 18;
export const BARRIER_PILL_HEIGHT_PX = 18;
export const BARRIER_ACK_TIMEOUT_MS = 10_000;

const TERMINAL_PHASES = new Set(["CLOSED", "CANCELLED", "LIQUIDATED"]);

export function compactBarrierAccountLabel(accountId: string, maxLen = 18): string {
  if (accountId.length <= maxLen) return accountId;
  return `${accountId.slice(0, maxLen - 1)}…`;
}

export function formatBarrierPrice(price: number, precision: number): string {
  const safePrec = Math.max(0, Math.min(8, Math.trunc(precision)));
  const factor = 10 ** safePrec;
  const rounded = Math.round(price * factor) / factor;
  if (safePrec === 0) return String(Math.round(rounded));
  const fixed = rounded.toFixed(safePrec);
  return fixed.replace(/\.?0+$/, "");
}

export function disabledReasonLabel(reason: BarrierDragDisabledReason): string {
  switch (reason) {
    case "OFFLINE":
      return t(
        "账户进程离线，已禁止拖动改单",
        "Account process offline — barrier drag disabled",
      );
    case "REPLAY":
      return t(
        "回放模式下已禁止拖动改单",
        "Replay mode active — barrier drag disabled",
      );
    case "HALTED":
      return t(
        "子账户处于停机 (HALTED) 状态，已禁止拖动改单",
        "Sub-account is HALTED — barrier drag disabled",
      );
    case "READ_ONLY":
      return t("当前为只读视图，已禁止拖动改单", "Read-only view — barrier drag disabled");
    case "NOT_ACTIVE_ROUND":
      return t("仅支持拖动活跃回合的止损/止盈线", "Only active rounds can be dragged");
  }
}

function resolveBarrierEntryPrice(barrier: DraggableBarrierSpec): number | null {
  if (barrier.entry_price != null && barrier.entry_price !== "") {
    const n = Number(barrier.entry_price);
    if (Number.isFinite(n)) return n;
  }
  if (barrier.entry_low != null && barrier.entry_high != null) {
    const lo = Number(barrier.entry_low);
    const hi = Number(barrier.entry_high);
    if (Number.isFinite(lo) && Number.isFinite(hi)) return (lo + hi) / 2;
  }
  return null;
}

/**
 * Classify a barrier drag strictly matching Ticket 04 / ADR-0015 rules:
 * - STOP moving in favorable direction => TIGHTEN_STOP (direct to FillSlot), crossesBreakeven when >= / <= entry.
 * - STOP moving in unfavorable direction => WIDEN_STOP (requires RiskSlot approval).
 * - TP moving farther from entry (LONG: max/min increases; SHORT: min/max decreases) => MODIFY_TAKE_PROFIT (widening, requires RiskSlot).
 * - TP pulling closer => MODIFY_TAKE_PROFIT (non-widening, direct to FillSlot).
 */
export function classifyBarrierDrag(
  barrier: DraggableBarrierSpec,
  kind: BarrierKind,
  tpIndex: number,
  proposedPriceText: string,
): BarrierDragClassification {
  const proposed = Number(proposedPriceText);
  const entry = resolveBarrierEntryPrice(barrier);
  const currentStop = Number(barrier.stop);
  const qty = barrier.quantity != null ? Number(barrier.quantity) : Number.NaN;
  const hasQty = Number.isFinite(qty) && qty > 0;

  if (kind === "STOP") {
    const isNoop = Number.isFinite(currentStop) && proposed === currentStop;
    const isWidening =
      barrier.direction === "LONG" ? proposed < currentStop : proposed > currentStop;
    const commandKind = isWidening ? "WIDEN_STOP" : "TIGHTEN_STOP";
    const crossesBreakeven =
      entry !== null &&
      (barrier.direction === "LONG" ? proposed >= entry : proposed <= entry);

    let isValidGeometry = Number.isFinite(proposed) && proposed > 0;
    let geometryError: string | undefined;
    if (!isValidGeometry) {
      geometryError = t("止损价格必须大于 0", "Stop price must be > 0");
    } else if (barrier.take_profit.length > 0) {
      const tpNums = barrier.take_profit.map(Number).filter(Number.isFinite);
      if (tpNums.length > 0) {
        if (barrier.direction === "LONG" && proposed >= Math.min(...tpNums)) {
          isValidGeometry = false;
          geometryError = t(
            "做多止损必须低于止盈价",
            "Long stop must stay below take-profit",
          );
        } else if (barrier.direction === "SHORT" && proposed <= Math.max(...tpNums)) {
          isValidGeometry = false;
          geometryError = t(
            "做空止损必须高于止盈价",
            "Short stop must stay above take-profit",
          );
        }
      }
    }

    let rDelta: number | null = null;
    let riskAmountDelta: number | null = null;
    if (entry !== null && Number.isFinite(currentStop) && Number.isFinite(proposed)) {
      const origRiskPerUnit =
        barrier.direction === "LONG" ? entry - currentStop : currentStop - entry;
      const newRiskPerUnit =
        barrier.direction === "LONG" ? entry - proposed : proposed - entry;
      if (origRiskPerUnit > 0) {
        rDelta = (newRiskPerUnit - origRiskPerUnit) / origRiskPerUnit;
      }
      if (hasQty) {
        riskAmountDelta = (newRiskPerUnit - origRiskPerUnit) * qty;
      }
    }

    return {
      commandKind,
      isWidening,
      requiresRiskCheck: isWidening,
      crossesBreakeven,
      isNoop,
      isValidGeometry,
      geometryError,
      rDelta,
      riskAmountDelta,
    };
  }

  // TP drag
  const currentTps = barrier.take_profit;
  const originalTp = Number(currentTps[tpIndex] ?? Number.NaN);
  const isNoop = Number.isFinite(originalTp) && proposed === originalTp;
  const nextTakeProfits = currentTps.map((tp, idx) =>
    idx === tpIndex ? proposedPriceText : tp,
  );

  const curNums = currentTps.map(Number).filter(Number.isFinite);
  const nextNums = nextTakeProfits.map(Number).filter(Number.isFinite);

  let isWidening = false;
  if (curNums.length === 0) {
    isWidening = true;
  } else if (nextNums.length > 0) {
    if (barrier.direction === "LONG") {
      isWidening =
        Math.max(...nextNums) > Math.max(...curNums) ||
        Math.min(...nextNums) > Math.min(...curNums);
    } else {
      isWidening =
        Math.min(...nextNums) < Math.min(...curNums) ||
        Math.max(...nextNums) < Math.max(...curNums);
    }
  }

  let isValidGeometry = Number.isFinite(proposed) && proposed > 0;
  let geometryError: string | undefined;
  if (!isValidGeometry) {
    geometryError = t("止盈价格必须大于 0", "Take-profit price must be > 0");
  } else if (barrier.direction === "LONG") {
    if (Number.isFinite(currentStop) && proposed <= currentStop) {
      isValidGeometry = false;
      geometryError = t("做多止盈必须高于止损价", "Long take-profit must be above stop");
    } else if (entry !== null && proposed <= entry) {
      isValidGeometry = false;
      geometryError = t("做多止盈必须高于入场价", "Long take-profit must be above entry");
    }
  } else {
    if (Number.isFinite(currentStop) && proposed >= currentStop) {
      isValidGeometry = false;
      geometryError = t("做空止盈必须低于止损价", "Short take-profit must be below stop");
    } else if (entry !== null && proposed >= entry) {
      isValidGeometry = false;
      geometryError = t("做空止盈必须低于入场价", "Short take-profit must be below entry");
    }
  }

  let rDelta: number | null = null;
  let riskAmountDelta: number | null = null;
  if (
    entry !== null &&
    Number.isFinite(currentStop) &&
    Number.isFinite(originalTp) &&
    Number.isFinite(proposed)
  ) {
    const origRiskPerUnit =
      barrier.direction === "LONG" ? entry - currentStop : currentStop - entry;
    const rewardDeltaPerUnit =
      barrier.direction === "LONG" ? proposed - originalTp : originalTp - proposed;
    if (origRiskPerUnit > 0) {
      rDelta = rewardDeltaPerUnit / origRiskPerUnit;
    }
    if (hasQty) {
      riskAmountDelta = rewardDeltaPerUnit * qty;
    }
  }

  return {
    commandKind: "MODIFY_TAKE_PROFIT",
    isWidening,
    requiresRiskCheck: isWidening,
    crossesBreakeven: false,
    isNoop,
    isValidGeometry,
    geometryError,
    rDelta,
    riskAmountDelta,
    nextTakeProfits,
  };
}

export function formatDragBadgeText(
  kind: BarrierKind,
  tpIndex: number,
  previewPrice: string,
  classification: BarrierDragClassification,
  phase: BarrierOverlayPhase,
): string {
  const prefix = kind === "STOP" ? "SL" : `TP${tpIndex + 1}`;
  if (phase === "pending_ack") {
    return `${prefix} ${previewPrice} · ${t("待执行…", "Pending…")}`;
  }
  if (!classification.isValidGeometry) {
    return `${prefix} ${previewPrice} · ${t("价格无效", "Invalid")}`;
  }
  const tags: string[] = [];
  if (classification.rDelta !== null && Number.isFinite(classification.rDelta)) {
    const sign = classification.rDelta > 0 ? "+" : "";
    const unit = kind === "STOP" ? "R" : "R";
    tags.push(`Δ${sign}${classification.rDelta.toFixed(2)}${unit}`);
  }
  if (classification.crossesBreakeven) {
    tags.push(t("保本", "Breakeven"));
  }
  if (classification.requiresRiskCheck) {
    tags.push(t("需风控审批", "Risk check"));
  } else if (!classification.isNoop) {
    tags.push(t("收紧直达", "Direct tighten"));
  }
  return tags.length > 0 ? `${prefix} ${previewPrice} · ${tags.join(" · ")}` : `${prefix} ${previewPrice}`;
}

export function resolveBarrierDisabledReason(
  barrier: DraggableBarrierSpec,
  env?: { readOnly?: boolean; replayActive?: boolean },
): BarrierDragDisabledReason | null {
  if (env?.replayActive) return "REPLAY";
  if (barrier.phase && TERMINAL_PHASES.has(barrier.phase)) return "NOT_ACTIVE_ROUND";
  if (barrier.disabled_reason) return barrier.disabled_reason;
  if (barrier.account_trading_state === "HALTED") return "HALTED";
  if (barrier.draggable === false) return "OFFLINE";
  if (env?.readOnly) return "READ_ONLY";
  return null;
}

function defaultMeasurePillWidth(text: string): number {
  return Math.ceil(text.length * 6.4) + 14;
}

/**
 * Shared pure layout helper used by BOTH `createExecutionBarrierLayer().render()`
 * and `BarrierDragController` hit-testing so pill hitboxes stay 100% pixel-aligned
 * even when multiple sub-accounts share the exact same stop or take-profit price.
 */
export function layoutBarrierTargets(
  barriers: readonly DraggableBarrierSpec[],
  priceToY: (price: number) => number,
  canvasWidth: number,
  options?: {
    measureTextWidth?: (text: string) => number;
    readOnly?: boolean;
    replayActive?: boolean;
  },
): BarrierHitTarget[] {
  const measureWidth = (text: string): number =>
    options?.measureTextWidth ? Math.ceil(options.measureTextWidth(text)) + 12 : defaultMeasurePillWidth(text);

  const targets: BarrierHitTarget[] = [];
  const placed: BarrierPillRect[] = [];

  const placePill = (lineY: number, label: string): BarrierPillRect => {
    const w = measureWidth(label);
    const h = BARRIER_PILL_HEIGHT_PX;
    let x = 12;
    for (const prev of placed) {
      const prevCenterY = prev.y + prev.h / 2;
      if (Math.abs(prevCenterY - lineY) < BARRIER_PILL_STAGGER_PX) {
        const rightEdge = prev.x + prev.w + 8;
        if (rightEdge > x) {
          x = rightEdge;
        }
      }
    }
    const clampedX = Math.max(8, Math.min(x, Math.max(8, canvasWidth - w - 8)));
    const rect: BarrierPillRect = { x: clampedX, y: lineY - h / 2, w, h };
    placed.push(rect);
    return rect;
  };

  for (const barrier of barriers) {
    const acct = compactBarrierAccountLabel(barrier.sub_account_id);
    const disabledReason = resolveBarrierDisabledReason(barrier, {
      readOnly: options?.readOnly,
      replayActive: options?.replayActive,
    });
    const draggable = disabledReason === null;

    // Stop loss target
    const stopPrice = Number(barrier.stop);
    if (Number.isFinite(stopPrice)) {
      const lineY = priceToY(stopPrice);
      if (Number.isFinite(lineY)) {
        const label = `${acct} · SL ${barrier.stop}`;
        targets.push({
          barrier,
          kind: "STOP",
          tpIndex: -1,
          price: stopPrice,
          priceText: barrier.stop,
          lineY,
          label,
          pillRect: placePill(lineY, label),
          draggable,
          disabledReason,
        });
      }
    }

    // Take profit targets
    for (let idx = 0; idx < barrier.take_profit.length; idx += 1) {
      const tpText = barrier.take_profit[idx];
      const tpPrice = Number(tpText);
      if (!Number.isFinite(tpPrice)) continue;
      const lineY = priceToY(tpPrice);
      if (!Number.isFinite(lineY)) continue;
      const tpTag = barrier.take_profit.length > 1 ? `TP${idx + 1}` : "TP";
      const label = `${acct} · ${tpTag} ${tpText}`;
      targets.push({
        barrier,
        kind: "TP",
        tpIndex: idx,
        price: tpPrice,
        priceText: tpText,
        lineY,
        label,
        pillRect: placePill(lineY, label),
        draggable,
        disabledReason,
      });
    }
  }

  return targets;
}

/**
 * Staggered pill-first hit-testing:
 * 1. Check pill rectangles (X + Y) first so overlapping sub-account barriers at the
 *    exact same price can each be grabbed via their staggered pill.
 * 2. Fall back to horizontal line proximity (Y within tolerancePx), preferring the
 *    currently active sub-account on ties.
 */
export function hitTestBarrierTargets(
  targets: readonly BarrierHitTarget[],
  cursorX: number,
  cursorY: number,
  options?: {
    activeSubAccountId?: string | null;
    tolerancePx?: number;
  },
): BarrierHitTarget | null {
  const tol = options?.tolerancePx ?? BARRIER_HIT_TOLERANCE_PX;
  const activeSub = options?.activeSubAccountId ?? null;

  // 1. Pill hit (X + Y)
  const pillHits: BarrierHitTarget[] = [];
  for (const target of targets) {
    const r = target.pillRect;
    if (
      cursorX >= r.x - 2 &&
      cursorX <= r.x + r.w + 2 &&
      cursorY >= r.y - 2 &&
      cursorY <= r.y + r.h + 2
    ) {
      pillHits.push(target);
    }
  }
  if (pillHits.length > 0) {
    if (activeSub) {
      const preferred = pillHits.find((h) => h.barrier.sub_account_id === activeSub);
      if (preferred) return preferred;
    }
    return pillHits[pillHits.length - 1];
  }

  // 2. Line proximity hit (Y)
  let best: { target: BarrierHitTarget; dist: number } | null = null;
  for (const target of targets) {
    const dist = Math.abs(cursorY - target.lineY);
    if (dist > tol) continue;
    if (!best) {
      best = { target, dist };
      continue;
    }
    if (Math.abs(dist - best.dist) <= 1.5 && activeSub) {
      const curActive = target.barrier.sub_account_id === activeSub;
      const bestActive = best.target.barrier.sub_account_id === activeSub;
      if (curActive && !bestActive) {
        best = { target, dist };
        continue;
      }
    }
    if (dist < best.dist) {
      best = { target, dist };
    }
  }
  return best?.target ?? null;
}

/**
 * Option B capture-phase barrier drag controller (per Ticket 06 `## Answer`):
 * - Bound to outer `container` (`{ capture: true }`) so WebGL2 -> Canvas2D
 *   `dataCanvas` replacement never breaks dragging.
 * - Strictly isolated from Vela `DrawingStore` / `DrawingHistory` (zero `HUMAN_DRAWING` pollution).
 * - Enforces D10 confirm-on-pointerup, 10s `pending_ack` revert timer, and offline/replay/HALTED guards.
 */
export class BarrierDragController {
  private readonly opts: BarrierDragControllerOptions;
  private overlay: BarrierDragOverlay | null = null;
  private hover: BarrierHoverState | null = null;
  private ackTimer: ReturnType<typeof setTimeout> | null = null;
  private activeDrag: {
    target: BarrierHitTarget;
    startY: number;
    movedPastSlop: boolean;
    canvasTop: number;
    latestPreviewPrice: string;
    latestClassification: BarrierDragClassification;
  } | null = null;
  private destroyed = false;

  private readonly handleContainerPointerDown: (e: PointerEvent) => void;
  private readonly handleContainerPointerMove: (e: PointerEvent) => void;
  private readonly handleContainerPointerLeave: () => void;
  private readonly handleWindowPointerMove: (e: PointerEvent) => void;
  private readonly handleWindowPointerUp: (e: PointerEvent) => void;
  private readonly handleWindowPointerCancel: () => void;
  private readonly handleWindowKeyDown: (e: KeyboardEvent) => void;

  constructor(options: BarrierDragControllerOptions) {
    this.opts = options;

    this.handleContainerPointerDown = (e: PointerEvent) => this.onPointerDown(e);
    this.handleContainerPointerMove = (e: PointerEvent) => this.onHoverMove(e);
    this.handleContainerPointerLeave = () => this.clearHover();
    this.handleWindowPointerMove = (e: PointerEvent) => this.onDragMove(e);
    this.handleWindowPointerUp = (e: PointerEvent) => this.onDragUp(e);
    this.handleWindowPointerCancel = () => this.cancelActiveDrag();
    this.handleWindowKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        this.cancelActiveDrag();
      }
    };

    // Capture phase on container survives WebGL2 -> Canvas2D dataCanvas swap!
    this.opts.container.addEventListener("pointerdown", this.handleContainerPointerDown, {
      capture: true,
    });
    // Bubble phase hover updates cursor after Vela's InputController.onMove to avoid flicker.
    this.opts.container.addEventListener("pointermove", this.handleContainerPointerMove);
    this.opts.container.addEventListener("pointerleave", this.handleContainerPointerLeave);
  }

  getOverlay(): BarrierDragOverlay | null {
    return this.overlay;
  }

  getHover(): BarrierHoverState | null {
    return this.hover;
  }

  /** Cancel a `pending_confirm` modal (D10): immediately snaps line back with zero command sent. */
  cancelPendingConfirm(): void {
    if (this.overlay?.phase === "pending_confirm") {
      this.setOverlay(null);
    }
  }

  /** Transition to optimistic `pending_ack` ("待执行") with 10s rollback timer. */
  markPendingAck(commandId: string, proposal: BarrierDragProposal): void {
    this.clearAckTimer();
    const next: BarrierDragOverlay = {
      roundId: proposal.roundId,
      subAccountId: proposal.subAccountId,
      direction: proposal.direction,
      kind: proposal.kind,
      tpIndex: proposal.tpIndex,
      originalPrice: proposal.originalPrice,
      previewPrice: proposal.targetPrice,
      phase: "pending_ack",
      badgeText: formatDragBadgeText(
        proposal.kind,
        proposal.tpIndex,
        proposal.targetPrice,
        proposal.classification,
        "pending_ack",
      ),
      classification: proposal.classification,
      commandId,
    };
    this.setOverlay(next);

    const timeoutMs = this.opts.ackTimeoutMs ?? BARRIER_ACK_TIMEOUT_MS;
    this.ackTimer = setTimeout(() => {
      this.ackTimer = null;
      if (this.destroyed) return;
      if (this.overlay?.phase === "pending_ack" && this.overlay.commandId === commandId) {
        const timedOut = this.overlay;
        this.setOverlay(null);
        this.opts.onPendingAckTimeout?.(timedOut);
      }
    }, timeoutMs);
  }

  /** Clear `pending_ack` (or `pending_confirm` on synchronous receipt) once command converges. */
  resolvePendingAck(commandId?: string): void {
    if (!this.overlay) return;
    if (this.overlay.phase === "pending_confirm") {
      this.clearAckTimer();
      this.setOverlay(null);
      return;
    }
    if (this.overlay.phase !== "pending_ack") return;
    if (commandId && this.overlay.commandId && this.overlay.commandId !== commandId) return;
    this.clearAckTimer();
    this.setOverlay(null);
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    this.detachWindowDragListeners();
    this.clearAckTimer();
    this.opts.container.removeEventListener("pointerdown", this.handleContainerPointerDown, {
      capture: true,
    });
    this.opts.container.removeEventListener("pointermove", this.handleContainerPointerMove);
    this.opts.container.removeEventListener("pointerleave", this.handleContainerPointerLeave);
    if (typeof document !== "undefined" && document.body.style.cursor === "ns-resize") {
      document.body.style.cursor = "";
    }
    this.overlay = null;
    this.hover = null;
  }

  private setOverlay(next: BarrierDragOverlay | null): void {
    this.overlay = next;
    this.opts.onOverlayChange(next);
  }

  private clearAckTimer(): void {
    if (this.ackTimer !== null) {
      clearTimeout(this.ackTimer);
      this.ackTimer = null;
    }
  }

  private resolvePointerLocal(e: PointerEvent): {
    x: number;
    y: number;
    top: number;
    proj: BarrierProjectionContext;
  } | null {
    const proj = this.opts.getProjection();
    if (!proj) return null;
    const rectEl =
      e.target instanceof HTMLCanvasElement ? e.target : this.opts.container;
    const rect = rectEl.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    if (
      x < 0 ||
      x > proj.coords.width ||
      y < proj.bounds.top ||
      y > proj.bounds.top + proj.bounds.height
    ) {
      return null;
    }
    return { x, y, top: rect.top, proj };
  }

  private hitTest(
    x: number,
    y: number,
    proj: BarrierProjectionContext,
  ): BarrierHitTarget | null {
    const barriers = this.opts.getBarriers();
    if (barriers.length === 0) return null;
    const targets = layoutBarrierTargets(
      barriers,
      (p) => proj.coords.priceToY(p, proj.scale, proj.bounds),
      proj.coords.width,
      {
        readOnly: this.opts.isReadOnly?.(),
        replayActive: this.opts.isReplayActive?.(),
      },
    );
    return hitTestBarrierTargets(targets, x, y, {
      activeSubAccountId: this.opts.getActiveSubAccountId?.() ?? null,
    });
  }

  private onHoverMove(e: PointerEvent): void {
    if (this.activeDrag || this.destroyed) return;
    if (!(e.target instanceof HTMLCanvasElement) || this.opts.isToolActive?.()) {
      this.clearHover();
      return;
    }
    const local = this.resolvePointerLocal(e);
    if (!local) {
      this.clearHover();
      return;
    }
    const hit = this.hitTest(local.x, local.y, local.proj);
    if (!hit) {
      this.clearHover();
      return;
    }
    const nextHover: BarrierHoverState = {
      roundId: hit.barrier.round_id,
      subAccountId: hit.barrier.sub_account_id,
      kind: hit.kind,
      tpIndex: hit.tpIndex,
      draggable: hit.draggable,
      disabledReason: hit.disabledReason,
    };
    const same =
      this.hover &&
      this.hover.roundId === nextHover.roundId &&
      this.hover.subAccountId === nextHover.subAccountId &&
      this.hover.kind === nextHover.kind &&
      this.hover.tpIndex === nextHover.tpIndex &&
      this.hover.draggable === nextHover.draggable &&
      this.hover.disabledReason === nextHover.disabledReason;
    if (!same) {
      this.hover = nextHover;
      this.opts.onHoverChange?.(nextHover);
    }
    e.target.style.cursor = hit.draggable ? "ns-resize" : "not-allowed";
  }

  private clearHover(): void {
    if (this.hover !== null) {
      this.hover = null;
      this.opts.onHoverChange?.(null);
    }
  }

  private onPointerDown(e: PointerEvent): void {
    if (this.destroyed) return;
    // Guard rails per Ticket 06 ## Answer §2.2
    if (e.button !== 0) return;
    if (!(e.target instanceof HTMLCanvasElement)) return;
    if (this.opts.isToolActive?.()) return;
    if (this.overlay?.phase === "pending_confirm" || this.overlay?.phase === "pending_ack") {
      return;
    }

    const local = this.resolvePointerLocal(e);
    if (!local) return;
    const hit = this.hitTest(local.x, local.y, local.proj);
    if (!hit) return;

    if (!hit.draggable) {
      const reason = hit.disabledReason ?? "OFFLINE";
      // Intercept disabled pill click so we show clear feedback without accidentally panning.
      e.stopPropagation();
      e.preventDefault();
      this.opts.onDisabledAttempt?.({
        barrier: hit.barrier,
        kind: hit.kind,
        reason,
        clientX: e.clientX,
        clientY: e.clientY,
      });
      return;
    }

    e.stopPropagation();
    e.preventDefault();

    const initialClassification = classifyBarrierDrag(
      hit.barrier,
      hit.kind,
      hit.tpIndex,
      hit.priceText,
    );

    this.activeDrag = {
      target: hit,
      startY: local.y,
      movedPastSlop: false,
      canvasTop: local.top,
      latestPreviewPrice: hit.priceText,
      latestClassification: initialClassification,
    };

    if (typeof document !== "undefined") {
      document.body.style.cursor = "ns-resize";
    }
    this.attachWindowDragListeners();
  }

  private onDragMove(e: PointerEvent): void {
    const drag = this.activeDrag;
    if (!drag || this.destroyed) return;
    const proj = this.opts.getProjection();
    if (!proj) return;

    const rawY = e.clientY - drag.canvasTop;
    const clampedY = Math.max(
      proj.bounds.top + 2,
      Math.min(proj.bounds.top + proj.bounds.height - 2, rawY),
    );

    if (!drag.movedPastSlop && Math.abs(clampedY - drag.startY) <= BARRIER_DRAG_SLOP_PX) {
      return;
    }
    drag.movedPastSlop = true;

    const rawPrice = proj.coords.yToPrice(clampedY, proj.scale, proj.bounds);
    if (!Number.isFinite(rawPrice) || rawPrice <= 0) return;

    const formatted = formatBarrierPrice(rawPrice, this.opts.getPricePrecision());
    const classification = classifyBarrierDrag(
      drag.target.barrier,
      drag.target.kind,
      drag.target.tpIndex,
      formatted,
    );

    drag.latestPreviewPrice = formatted;
    drag.latestClassification = classification;

    this.setOverlay({
      roundId: drag.target.barrier.round_id,
      subAccountId: drag.target.barrier.sub_account_id,
      direction: drag.target.barrier.direction,
      kind: drag.target.kind,
      tpIndex: drag.target.tpIndex,
      originalPrice: drag.target.priceText,
      previewPrice: formatted,
      phase: "dragging",
      badgeText: formatDragBadgeText(
        drag.target.kind,
        drag.target.tpIndex,
        formatted,
        classification,
        "dragging",
      ),
      classification,
    });
  }

  private onDragUp(_e: PointerEvent): void {
    const drag = this.activeDrag;
    if (!drag) return;
    this.activeDrag = null;
    this.detachWindowDragListeners();
    if (typeof document !== "undefined" && document.body.style.cursor === "ns-resize") {
      document.body.style.cursor = "";
    }

    const { target, movedPastSlop, latestPreviewPrice, latestClassification } = drag;
    if (
      !movedPastSlop ||
      latestClassification.isNoop ||
      !latestClassification.isValidGeometry
    ) {
      this.setOverlay(null);
      return;
    }

    // Enter pending_confirm state (spec D10): keep ghost + preview line until user confirms or cancels.
    this.setOverlay({
      roundId: target.barrier.round_id,
      subAccountId: target.barrier.sub_account_id,
      direction: target.barrier.direction,
      kind: target.kind,
      tpIndex: target.tpIndex,
      originalPrice: target.priceText,
      previewPrice: latestPreviewPrice,
      phase: "pending_confirm",
      badgeText: formatDragBadgeText(
        target.kind,
        target.tpIndex,
        latestPreviewPrice,
        latestClassification,
        "pending_confirm",
      ),
      classification: latestClassification,
    });

    const payload: PaperCommandPayload =
      target.kind === "STOP"
        ? { stop: latestPreviewPrice }
        : { take_profit: latestClassification.nextTakeProfits ?? [latestPreviewPrice] };

    this.opts.onDragEnd({
      roundId: target.barrier.round_id,
      subAccountId: target.barrier.sub_account_id,
      direction: target.barrier.direction,
      kind: target.kind,
      tpIndex: target.tpIndex,
      originalPrice: target.priceText,
      targetPrice: latestPreviewPrice,
      entryPrice: target.barrier.entry_price ?? null,
      basedOnLedgerSeq: target.barrier.ledger_seq ?? 0,
      classification: latestClassification,
      payload,
    });
  }

  private cancelActiveDrag(): void {
    if (!this.activeDrag) return;
    this.activeDrag = null;
    this.detachWindowDragListeners();
    if (typeof document !== "undefined" && document.body.style.cursor === "ns-resize") {
      document.body.style.cursor = "";
    }
    this.setOverlay(null);
  }

  private attachWindowDragListeners(): void {
    if (typeof window === "undefined") return;
    window.addEventListener("pointermove", this.handleWindowPointerMove);
    window.addEventListener("pointerup", this.handleWindowPointerUp);
    window.addEventListener("pointercancel", this.handleWindowPointerCancel);
    window.addEventListener("keydown", this.handleWindowKeyDown);
  }

  private detachWindowDragListeners(): void {
    if (typeof window === "undefined") return;
    window.removeEventListener("pointermove", this.handleWindowPointerMove);
    window.removeEventListener("pointerup", this.handleWindowPointerUp);
    window.removeEventListener("pointercancel", this.handleWindowPointerCancel);
    window.removeEventListener("keydown", this.handleWindowKeyDown);
  }
}
