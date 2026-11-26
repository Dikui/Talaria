import { afterEach, describe, expect, it, vi } from "vitest";
import { Vela, type OHLCV } from "@luxalgo/vela";
import type { DraftRecord, DrawingRecord } from "@talaria/contracts";

import {
  ALERT_ACTION_ID,
  DRAFT_ACTION_ID,
  VelaDrawingSyncBridge,
  bindChartDrawingActions,
  isEphemeralOrderDrawing,
} from "./drawingBridge";
import {
  OrderGestureTracker,
  extractPositionGeometry,
  isValidOrderGeometry,
  simulatePositionHandleDrag,
  type OrderGestureGeometry,
} from "./orderGestureTracker";
import {
  LAYER_STAGED_DRAFT,
  clearChartLayerState,
  ensureTalariaLayersRegistered,
  getLatestProjection,
  hitTestStagedDraft,
  isNonTerminalDraft,
  setChartLayerChannelData,
} from "./velaLayers";

if (typeof globalThis.PointerEvent === "undefined") {
  class PolyfillPointerEvent extends MouseEvent {
    pointerId: number;
    pointerType: string;
    constructor(type: string, init: PointerEventInit = {}) {
      super(type, init);
      this.pointerId = init.pointerId ?? 1;
      this.pointerType = init.pointerType ?? "mouse";
    }
  }
  globalThis.PointerEvent = PolyfillPointerEvent as unknown as typeof PointerEvent;
}
if (typeof Element !== "undefined" && typeof Element.prototype.animate !== "function") {
  Element.prototype.animate = (() => ({
    cancel: () => undefined,
    finish: () => undefined,
    pause: () => undefined,
    play: () => undefined,
    onfinish: null,
  })) as unknown as typeof Element.prototype.animate;
}
if (typeof HTMLCanvasElement !== "undefined") {
  const probe = document
    .createElement("canvas")
    .getContext("2d") as unknown as Record<string, unknown> | null;
  if (probe && typeof probe.roundRect !== "function") {
    probe.roundRect = vi.fn();
  }
}

function makeBars(count = 60): OHLCV[] {
  const start = 1_700_000_000_000;
  const bars: OHLCV[] = [];
  for (let i = 0; i < count; i += 1) {
    const base = 68000 + Math.sin(i / 5) * 400;
    bars.push({
      time: start + i * 60_000,
      open: base,
      high: base + 120,
      low: base - 120,
      close: base + 30,
      volume: 100,
    });
  }
  return bars;
}

function mockElementRect(el: HTMLElement, width = 800, height = 400): void {
  Object.defineProperty(el, "clientWidth", { configurable: true, value: width });
  Object.defineProperty(el, "clientHeight", { configurable: true, value: height });
  el.getBoundingClientRect = () =>
    ({
      left: 0,
      top: 0,
      right: width,
      bottom: height,
      width,
      height,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    }) as DOMRect;
}

function dispatchPointer(
  target: EventTarget,
  type: "pointerdown" | "pointermove" | "pointerup",
  x: number,
  y: number,
): void {
  const ev = new PointerEvent(type, {
    bubbles: true,
    cancelable: true,
    clientX: x,
    clientY: y,
    button: 0,
    buttons: type === "pointerup" ? 0 : 1,
    pointerId: 1,
    pointerType: "mouse",
  });
  target.dispatchEvent(ev);
}

describe("OrderGestureTracker, ephemeral position isolation, and LAYER_STAGED_DRAFT", () => {
  const cleanups: Array<() => void> = [];

  afterEach(() => {
    while (cleanups.length > 0) {
      cleanups.pop()?.();
    }
    vi.useRealTimers();
    document.body.innerHTML = "";
  });

  async function mountLiveVela(chartId: string) {
    ensureTalariaLayersRegistered();
    const container = document.createElement("div");
    mockElementRect(container, 800, 400);
    document.body.appendChild(container);

    const bars = makeBars(60);
    const chart = new Vela(container, {
      symbol: "BTC-USDT-PERP",
      timeframe: "1m",
      data: bars,
      live: false,
      nativeBackend: "canvas2d",
      drawings: { toolbar: true },
      animations: { intro: false, liveBar: false },
    });
    chart.drawings.setToolDefaults({ position: { props: { showLossSize: false } } });

    for (const el of container.querySelectorAll("div, canvas")) {
      mockElementRect(el as HTMLElement, 800, 400);
    }
    chart.resize();

    chart.addNativeIndicator("SYSTEM_LEVEL", { inputs: { chartId } });
    chart.addNativeIndicator(LAYER_STAGED_DRAFT, { inputs: { chartId } });
    setChartLayerChannelData(chartId, "SYSTEM_LEVEL", {
      chartId,
      levels: [],
      precision: 1,
      highlightId: null,
    });

    await new Promise((r) => setTimeout(r, 30));

    const dataCanvas = [...container.querySelectorAll("canvas")].find(
      (c) => c.style.pointerEvents !== "none",
    )!;
    expect(dataCanvas).toBeDefined();

    cleanups.push(() => {
      clearChartLayerState(chartId);
      chart.destroy();
      container.remove();
    });

    return { chart, container, dataCanvas, bars };
  }

  it("validates strict geometry after precision rounding and mirrors constrainHandleDrag", () => {
    expect(isValidOrderGeometry("LONG", "68000.0", "67800.0", "68400.0")).toBe(true);
    expect(isValidOrderGeometry("LONG", "68000.0", "68000.0", "68400.0")).toBe(false);
    expect(isValidOrderGeometry("SHORT", "68000.0", "68200.0", "67600.0")).toBe(true);
    expect(isValidOrderGeometry("SHORT", "68000.0", "67800.0", "67600.0")).toBe(false);

    const orig = [
      { time: 1000, price: 68000 },
      { time: 2000, price: 67800 },
      { time: 2000, price: 68400 },
    ];
    // Dragging stop (index 1) above entry flips target across entry -> SHORT
    const flipped = simulatePositionHandleDrag(orig, 1, 68200, 2000, 67800, 2000);
    expect(flipped[1].price).toBe(68200);
    expect(flipped[2].price).toBe(67600);

    // Body drag (-1) translates all anchors equally
    const translated = simulatePositionHandleDrag(orig, -1, 68100, 2500, 68000, 2000);
    expect(translated.map((a) => a.price)).toEqual([68100, 67900, 68500]);
  });

  it("excludes ephemeral 'position' completely from HUMAN_DRAWING persistence, cleans undo stack, and emits live Shadow Handle-Drag frames", async () => {
    const { chart, container, dataCanvas, bars } = await mountLiveVela("t07-live-1");

    const createDrawing = vi.fn().mockResolvedValue({
      drawing_id: "dw-remote-1",
      instrument_id: "BTC-USDT-PERP",
      kind: "HORIZONTAL_LINE",
      visible_timeframes: ["1m"],
      points: [{ ts: bars[10].time, price: "68100.0" }],
      text: "",
      style: { color: "#4c8dff", line_width: 1, line_style: "solid", fill_opacity: 0.12 },
      version: 1,
      created_ts: bars[10].time,
      updated_ts: bars[10].time,
      deleted_at: null,
    } satisfies DrawingRecord);
    const patchDrawing = vi.fn();
    const deleteDrawing = vi.fn();

    const bridge = new VelaDrawingSyncBridge({
      chart,
      instrumentId: "BTC-USDT-PERP",
      timeframe: "1m",
      pricePrecision: 1,
      client: {
        drawings: vi.fn().mockResolvedValue({ drawings: [] }),
        createDrawing,
        patchDrawing,
        deleteDrawing,
      },
    });
    cleanups.push(() => bridge.destroy());

    const geometries: Array<OrderGestureGeometry | null> = [];
    const tracker = new OrderGestureTracker({
      chart,
      container,
      chartId: "t07-live-1",
      getPricePrecision: () => 1,
      onGestureChange: (g) => geometries.push(g),
    });
    cleanups.push(() => tracker.destroy());

    // Arm LONG order gesture, place interactively
    tracker.armOrderTool("LONG");
    dispatchPointer(dataCanvas, "pointerdown", 300, 200);
    dispatchPointer(dataCanvas, "pointerup", 300, 200);
    dispatchPointer(dataCanvas, "pointermove", 450, 120);
    dispatchPointer(dataCanvas, "pointerdown", 450, 120);
    dispatchPointer(dataCanvas, "pointerup", 450, 120);

    // Wait past drawingBridge debounce (240ms) to prove ZERO persistence calls fire
    await new Promise((r) => setTimeout(r, 300));
    expect(createDrawing).not.toHaveBeenCalled();
    expect(patchDrawing).not.toHaveBeenCalled();
    expect(deleteDrawing).not.toHaveBeenCalled();

    const placed = chart.drawings.all().find((d) => isEphemeralOrderDrawing(d))!;
    expect(placed).toBeDefined();
    expect(placed.props?.showLossSize).toBe(false);
    expect(bridge.exportHumanDrawingsDocument().drawings).toHaveLength(0);

    // Drag stop handle mid-drag and verify Shadow Handle-Drag Tracker emits live 'dragging' frames (<5ms)
    const proj = getLatestProjection("t07-live-1")!;
    const stopX = proj.coords.timeToX(placed.anchors[1].time);
    const stopY = proj.coords.priceToY(placed.anchors[1].price, proj.scale, proj.bounds);
    const nextY = stopY - 25;

    const t0 = performance.now();
    dispatchPointer(dataCanvas, "pointerdown", stopX, stopY);
    dispatchPointer(window, "pointermove", stopX, nextY);
    const dragCalcMs = performance.now() - t0;
    expect(dragCalcMs).toBeLessThan(50);

    const draggingFrames = geometries.filter((g) => g?.phase === "dragging");
    expect(draggingFrames.length).toBeGreaterThanOrEqual(1);
    expect(draggingFrames[0]!.stop).toBeGreaterThan(placed.anchors[1].price);

    dispatchPointer(dataCanvas, "pointerup", stopX, nextY);
    dispatchPointer(window, "pointerup", stopX, nextY);

    await new Promise((r) => setTimeout(r, 300));
    expect(createDrawing).not.toHaveBeenCalled();
    expect(patchDrawing).not.toHaveBeenCalled();

    // Clear ephemeral drawings -> undo() must NOT resurrect the position
    bridge.clearEphemeralDrawings();
    tracker.clear();
    expect(chart.drawings.all().some((d) => d.type === "position")).toBe(false);
    expect(chart.drawings.canUndo()).toBe(false);
    chart.drawings.undo();
    expect(chart.drawings.all().some((d) => d.type === "position")).toBe(false);
  });

  it("flips bare-click placement to SHORT when armOrderTool('SHORT') was used and wires unified drawing actions", async () => {
    const { chart, container, dataCanvas, bars } = await mountLiveVela("t07-live-2");

    const tracker = new OrderGestureTracker({
      chart,
      container,
      chartId: "t07-live-2",
      getPricePrecision: () => 1,
    });
    cleanups.push(() => tracker.destroy());

    const onCreateAlert = vi.fn();
    const onCreateDraft = vi.fn();
    const unbind = bindChartDrawingActions(chart, { onCreateAlert, onCreateDraft });
    cleanups.push(unbind);

    // Arm SHORT and bare-click twice at same spot (<6px drag -> Vela defaults LONG, tracker flips to SHORT)
    tracker.armOrderTool("SHORT");
    dispatchPointer(dataCanvas, "pointerdown", 300, 200);
    dispatchPointer(dataCanvas, "pointerup", 300, 200);
    dispatchPointer(dataCanvas, "pointerdown", 301, 200);
    dispatchPointer(dataCanvas, "pointerup", 301, 200);

    const pos = chart.drawings.all().find((d) => d.type === "position")!;
    expect(pos).toBeDefined();
    const geom = extractPositionGeometry(pos, 1, "placed")!;
    expect(geom.direction).toBe("SHORT");
    expect(geom.stop).toBeGreaterThan(geom.entry);
    expect(geom.target).toBeLessThan(geom.entry);

    // Position toolbar shows Create Draft and hides Create Price Alert
    chart.drawings.openSettings(pos.id);
    const draftBtn = container.querySelector(
      `[data-drawing-action="${DRAFT_ACTION_ID}"]`,
    ) as HTMLButtonElement | null;
    const alertBtnOnPos = container.querySelector(
      `[data-drawing-action="${ALERT_ACTION_ID}"]`,
    );
    expect(draftBtn).not.toBeNull();
    expect(alertBtnOnPos).toBeNull();

    draftBtn!.click();
    expect(onCreateDraft).toHaveBeenCalledTimes(1);
    expect(onCreateDraft.mock.calls[0][0].id).toBe(pos.id);

    // Normal hline shows Create Price Alert and hides Create Draft
    const hline = chart.drawings.add("hline", {
      anchors: [{ time: bars[10].time, price: 68100 }],
    })!;
    chart.drawings.openSettings(hline.id);
    expect(container.querySelector(`[data-drawing-action="${DRAFT_ACTION_ID}"]`)).toBeNull();
    const alertBtn = container.querySelector(
      `[data-drawing-action="${ALERT_ACTION_ID}"]`,
    ) as HTMLButtonElement | null;
    expect(alertBtn).not.toBeNull();
    alertBtn!.click();
    expect(onCreateAlert).toHaveBeenCalledTimes(1);
  });

  it("hit-tests non-terminal STAGED_DRAFT entry band, stop, and multi-TP while ignoring terminal drafts", () => {
    const scale = { min: 67000, max: 69000, log: false, invert: false };
    const bounds = { top: 0, height: 400 };
    const coords = {
      priceToY: (price: number) => ((69000 - price) / 2000) * 400,
    };

    const baseContent = {
      sub_account_id: "sa-paper",
      instrument_id: "BTC-USDT-PERP",
      snapshot_id: "snap-1",
      direction: "LONG" as const,
      entry_low: "68000.0",
      entry_high: "68050.0",
      invalidation_stop: "67700.0",
      take_profit: ["68500.0", "68800.0"],
      valid_until: null,
      consultation_id: null,
      round_id: null,
      inherited_barriers: null,
    };

    const staged: DraftRecord = {
      draft_id: "drf-active",
      version: 1,
      content_hash: "a".repeat(64),
      status: "STAGED",
      claimed: false,
      receipt: null,
      content: baseContent,
    };
    const executed: DraftRecord = {
      ...staged,
      draft_id: "drf-done",
      status: "CONFIRMED",
      claimed: true,
      receipt: {
        draft_id: "drf-done",
        version: 1,
        accepted: true,
        plan_id: "plan-1",
      },
    };
    const discarded: DraftRecord = {
      ...staged,
      draft_id: "drf-disc",
      status: "DISCARDED",
    };

    expect(isNonTerminalDraft(staged)).toBe(true);
    expect(isNonTerminalDraft(executed)).toBe(false);
    expect(isNonTerminalDraft(discarded)).toBe(false);

    // Hit entry band, SL, and TP2 on active draft; terminal drafts never hit
    expect(
      hitTestStagedDraft([executed, discarded, staged], coords.priceToY(68025), coords, scale, bounds)
        ?.draft_id,
    ).toBe("drf-active");
    expect(
      hitTestStagedDraft([executed, discarded, staged], coords.priceToY(67700) + 2, coords, scale, bounds)
        ?.draft_id,
    ).toBe("drf-active");
    expect(
      hitTestStagedDraft([executed, discarded, staged], coords.priceToY(68800) - 2, coords, scale, bounds)
        ?.draft_id,
    ).toBe("drf-active");
    expect(
      hitTestStagedDraft([executed, discarded], coords.priceToY(68025), coords, scale, bounds),
    ).toBeNull();
  });
});
