import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setLocale } from "../lib/i18n";
import {
  BarrierDragController,
  classifyBarrierDrag,
  hitTestBarrierTargets,
  layoutBarrierTargets,
  type BarrierDragOverlay,
  type BarrierDragProposal,
  type BarrierProjectionContext,
  type DraggableBarrierSpec,
} from "./barrierDragController";

function makeLinearProjection(
  minPrice = 48000,
  maxPrice = 52000,
  height = 400,
  width = 800,
): BarrierProjectionContext {
  const top = 0;
  const span = maxPrice - minPrice;
  return {
    coords: {
      width,
      height,
      priceToY: (price: number) => top + ((maxPrice - price) / span) * height,
      yToPrice: (y: number) => maxPrice - ((y - top) / height) * span,
    },
    scale: {},
    bounds: { top, height },
  };
}

function makeBarrier(overrides: Partial<DraggableBarrierSpec> = {}): DraggableBarrierSpec {
  return {
    round_id: "round-1",
    sub_account_id: "sa-alpha",
    direction: "LONG",
    stop: "49000",
    take_profit: ["51500"],
    phase: "ACTIVE_PROTECTED",
    entry_price: "50000",
    quantity: "0.5",
    ledger_seq: 12,
    draggable: true,
    disabled_reason: null,
    account_trading_state: "ACTIVE",
    ...overrides,
  };
}

function makePointerEvent(
  type: string,
  init: { clientX: number; clientY: number; button?: number },
): PointerEvent {
  // jsdom may not define PointerEvent constructor; fallback to MouseEvent with pointer properties.
  const Evt = typeof PointerEvent !== "undefined" ? PointerEvent : MouseEvent;
  return new Evt(type, {
    bubbles: true,
    cancelable: true,
    clientX: init.clientX,
    clientY: init.clientY,
    button: init.button ?? 0,
  }) as PointerEvent;
}

describe("Ticket 08: barrierDragController", () => {
  beforeEach(() => {
    setLocale("en");
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("classifies STOP tighten/breakeven/widen and TP widen/tighten identically to Ticket 04 rules", () => {
    const longBarrier = makeBarrier();

    // LONG stop tighten (below entry): direct, no risk check, rDelta < 0
    const tight = classifyBarrierDrag(longBarrier, "STOP", -1, "49500");
    expect(tight.commandKind).toBe("TIGHTEN_STOP");
    expect(tight.isWidening).toBe(false);
    expect(tight.requiresRiskCheck).toBe(false);
    expect(tight.crossesBreakeven).toBe(false);
    expect(tight.rDelta).toBeCloseTo(-0.5, 5);

    // LONG stop moved to/past entry => crossesBreakeven = true
    const be = classifyBarrierDrag(longBarrier, "STOP", -1, "50100");
    expect(be.commandKind).toBe("TIGHTEN_STOP");
    expect(be.crossesBreakeven).toBe(true);
    expect(be.isValidGeometry).toBe(true);
    expect(be.rDelta).toBeCloseTo(-1.1, 5);

    // LONG stop widened (lowered) => WIDEN_STOP + requiresRiskCheck
    const widen = classifyBarrierDrag(longBarrier, "STOP", -1, "48500");
    expect(widen.commandKind).toBe("WIDEN_STOP");
    expect(widen.isWidening).toBe(true);
    expect(widen.requiresRiskCheck).toBe(true);
    expect(widen.rDelta).toBeCloseTo(0.5, 5);

    // LONG TP pushed farther => widening (requiresRiskCheck)
    const tpWiden = classifyBarrierDrag(longBarrier, "TP", 0, "51800");
    expect(tpWiden.commandKind).toBe("MODIFY_TAKE_PROFIT");
    expect(tpWiden.isWidening).toBe(true);
    expect(tpWiden.requiresRiskCheck).toBe(true);
    expect(tpWiden.nextTakeProfits).toEqual(["51800"]);

    // LONG TP pulled closer => non-widening (direct)
    const tpTight = classifyBarrierDrag(longBarrier, "TP", 0, "51000");
    expect(tpTight.commandKind).toBe("MODIFY_TAKE_PROFIT");
    expect(tpTight.isWidening).toBe(false);
    expect(tpTight.requiresRiskCheck).toBe(false);

    // SHORT stop & TP symmetry
    const shortBarrier = makeBarrier({
      direction: "SHORT",
      entry_price: "50000",
      stop: "51000",
      take_profit: ["48500"],
    });
    expect(classifyBarrierDrag(shortBarrier, "STOP", -1, "50000")).toMatchObject({
      commandKind: "TIGHTEN_STOP",
      crossesBreakeven: true,
      requiresRiskCheck: false,
    });
    expect(classifyBarrierDrag(shortBarrier, "STOP", -1, "51500")).toMatchObject({
      commandKind: "WIDEN_STOP",
      requiresRiskCheck: true,
    });
    expect(classifyBarrierDrag(shortBarrier, "TP", 0, "48000")).toMatchObject({
      commandKind: "MODIFY_TAKE_PROFIT",
      isWidening: true,
      requiresRiskCheck: true,
    });
    expect(classifyBarrierDrag(shortBarrier, "TP", 0, "49000")).toMatchObject({
      commandKind: "MODIFY_TAKE_PROFIT",
      isWidening: false,
      requiresRiskCheck: false,
    });
  });

  it("staggers overlapping multi-sub-account pills at identical prices and prefers pill X+Y hit over line", () => {
    const proj = makeLinearProjection();
    const b1 = makeBarrier({ round_id: "r-1", sub_account_id: "sa-first", stop: "49000" });
    const b2 = makeBarrier({ round_id: "r-2", sub_account_id: "sa-second", stop: "49000" });

    const targets = layoutBarrierTargets(
      [b1, b2],
      (p) => proj.coords.priceToY(p, proj.scale, proj.bounds),
      800,
    );
    const stop1 = targets.find((t) => t.barrier.sub_account_id === "sa-first" && t.kind === "STOP")!;
    const stop2 = targets.find((t) => t.barrier.sub_account_id === "sa-second" && t.kind === "STOP")!;

    // Pills at identical Y are horizontally staggered with no overlap.
    expect(stop1.lineY).toBe(stop2.lineY);
    expect(stop2.pillRect.x).toBeGreaterThanOrEqual(stop1.pillRect.x + stop1.pillRect.w + 8);

    // Clicking inside pill 1 hits sa-first; clicking inside pill 2 hits sa-second.
    const hit1 = hitTestBarrierTargets(
      targets,
      stop1.pillRect.x + 10,
      stop1.lineY,
      { activeSubAccountId: "sa-second" },
    );
    const hit2 = hitTestBarrierTargets(
      targets,
      stop2.pillRect.x + 10,
      stop2.lineY,
      { activeSubAccountId: "sa-first" },
    );
    expect(hit1?.barrier.sub_account_id).toBe("sa-first");
    expect(hit2?.barrier.sub_account_id).toBe("sa-second");

    // Clicking empty line space breaks ties in favor of activeSubAccountId.
    const lineHit = hitTestBarrierTargets(targets, 650, stop1.lineY, {
      activeSubAccountId: "sa-second",
    });
    expect(lineHit?.barrier.sub_account_id).toBe("sa-second");
  });

  it("handles capture-phase drag surviving canvas swap, Escape cancel, D10 confirm cancel, and 10s pending_ack timeout", () => {
    const container = document.createElement("div");
    const canvas1 = document.createElement("canvas");
    canvas1.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 800, height: 400, right: 800, bottom: 400, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
    container.appendChild(canvas1);
    document.body.appendChild(container);

    const proj = makeLinearProjection(48000, 52000, 400, 800);
    const barrier = makeBarrier({ stop: "49000", entry_price: "50000", ledger_seq: 18 });

    const overlays: Array<BarrierDragOverlay | null> = [];
    const proposals: BarrierDragProposal[] = [];
    const timeouts: BarrierDragOverlay[] = [];

    // Bubble listener on canvas verifies stopPropagation prevents Vela pan / drawing select.
    const bubbleSpy = vi.fn();
    canvas1.addEventListener("pointerdown", bubbleSpy);

    const controller = new BarrierDragController({
      container,
      getProjection: () => proj,
      getBarriers: () => [barrier],
      getPricePrecision: () => 1,
      onOverlayChange: (ov) => overlays.push(ov),
      onDragEnd: (p) => proposals.push(p),
      onPendingAckTimeout: (ov) => timeouts.push(ov),
      ackTimeoutMs: 10_000,
    });

    // 1. Start drag at stop=49000 (y=300), move to y=200 (price=50000), press Escape -> cancels with zero proposal.
    canvas1.dispatchEvent(makePointerEvent("pointerdown", { clientX: 50, clientY: 300 }));
    expect(bubbleSpy).not.toHaveBeenCalled();
    window.dispatchEvent(makePointerEvent("pointermove", { clientX: 50, clientY: 200 }));
    expect(overlays.at(-1)?.phase).toBe("dragging");
    expect(overlays.at(-1)?.previewPrice).toBe("50000");
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(overlays.at(-1)).toBeNull();
    expect(proposals).toHaveLength(0);

    // 2. Simulate WebGL2 -> Canvas2D fallback replacing dataCanvas inside container!
    container.removeChild(canvas1);
    const canvas2 = document.createElement("canvas");
    canvas2.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 800, height: 400, right: 800, bottom: 400, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
    container.appendChild(canvas2);

    // Drag on new canvas2 from y=300 (49000) to y=250 (49500) -> pointerup enters pending_confirm!
    canvas2.dispatchEvent(makePointerEvent("pointerdown", { clientX: 50, clientY: 300 }));
    window.dispatchEvent(makePointerEvent("pointermove", { clientX: 50, clientY: 250 }));
    window.dispatchEvent(makePointerEvent("pointerup", { clientX: 50, clientY: 250 }));

    expect(proposals).toHaveLength(1);
    expect(proposals[0]).toMatchObject({
      roundId: "round-1",
      subAccountId: "sa-alpha",
      kind: "STOP",
      originalPrice: "49000",
      targetPrice: "49500",
      basedOnLedgerSeq: 18,
      payload: { stop: "49500" },
    });
    expect(overlays.at(-1)?.phase).toBe("pending_confirm");

    // Cancel from D10 modal reverts overlay immediately.
    controller.cancelPendingConfirm();
    expect(overlays.at(-1)).toBeNull();

    // Synchronous rejection while still in pending_confirm also reverts overlay via resolvePendingAck.
    canvas2.dispatchEvent(makePointerEvent("pointerdown", { clientX: 50, clientY: 300 }));
    window.dispatchEvent(makePointerEvent("pointermove", { clientX: 50, clientY: 350 }));
    window.dispatchEvent(makePointerEvent("pointerup", { clientX: 50, clientY: 350 }));
    expect(overlays.at(-1)?.phase).toBe("pending_confirm");
    controller.resolvePendingAck("cmd-sync-reject");
    expect(overlays.at(-1)).toBeNull();

    // 3. Confirm -> enters pending_ack; after 10_000ms without receipt, reverts and notifies timeout!
    controller.markPendingAck("cmd-1", proposals[0]);
    expect(overlays.at(-1)?.phase).toBe("pending_ack");
    vi.advanceTimersByTime(10_000);
    expect(overlays.at(-1)).toBeNull();
    expect(timeouts).toHaveLength(1);
    expect(timeouts[0].commandId).toBe("cmd-1");

    controller.destroy();
    document.body.removeChild(container);
  });

  it("blocks drag with explicit disabled reason when offline, replay, or HALTED", () => {
    const container = document.createElement("div");
    const canvas = document.createElement("canvas");
    canvas.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 800, height: 400, right: 800, bottom: 400, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
    container.appendChild(canvas);
    document.body.appendChild(container);

    const proj = makeLinearProjection(48000, 52000, 400, 800);
    let currentBarrier = makeBarrier({ draggable: false, disabled_reason: "OFFLINE" });
    let replayActive = false;

    const disabledEvents: string[] = [];
    const proposals: BarrierDragProposal[] = [];

    const controller = new BarrierDragController({
      container,
      getProjection: () => proj,
      getBarriers: () => [currentBarrier],
      getPricePrecision: () => 1,
      isReplayActive: () => replayActive,
      onOverlayChange: () => undefined,
      onDragEnd: (p) => proposals.push(p),
      onDisabledAttempt: (ev) => disabledEvents.push(ev.reason),
    });

    // Offline
    canvas.dispatchEvent(makePointerEvent("pointerdown", { clientX: 50, clientY: 300 }));
    expect(disabledEvents.at(-1)).toBe("OFFLINE");

    // HALTED
    currentBarrier = makeBarrier({ draggable: true, disabled_reason: null, account_trading_state: "HALTED" });
    canvas.dispatchEvent(makePointerEvent("pointerdown", { clientX: 50, clientY: 300 }));
    expect(disabledEvents.at(-1)).toBe("HALTED");

    // Replay
    currentBarrier = makeBarrier({ draggable: true, disabled_reason: null, account_trading_state: "ACTIVE" });
    replayActive = true;
    canvas.dispatchEvent(makePointerEvent("pointerdown", { clientX: 50, clientY: 300 }));
    expect(disabledEvents.at(-1)).toBe("REPLAY");
    expect(proposals).toHaveLength(0);

    controller.destroy();
    document.body.removeChild(container);
  });
});
