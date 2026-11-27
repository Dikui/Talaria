import { describe, expect, it, vi } from "vitest";
import type {
  AgentAnnotationRecord,
  DrawingRecord,
  IndicatorSeries,
  KeyLevel,
  StructureFeature,
} from "@talaria/contracts";
import { drawingActions } from "@luxalgo/vela/plugin";

import {
  VelaDrawingSyncBridge,
  contractDrawingToSerialized,
  ensureDrawingAlertActionRegistered,
  serializedDrawingToCreate,
} from "./drawingBridge";
import {
  hitTestAgentAnnotation,
  hitTestSystemLevel,
  hitTestSystemStructure,
} from "./velaLayers";
import { alignIndicatorChannelPoints } from "../lib/indicatorData";

describe("4-layer isolation, authoritative indicators, and drawing persistence", () => {
  const scale = { min: 67000, max: 69000, log: false, invert: false };
  const bounds = { top: 0, height: 400 };
  const coords = {
    width: 800,
    timeToX: (ts: number) => (ts - 1_700_000_000_000) / 1000,
    priceToY: (price: number) => ((69000 - price) / 2000) * 400,
  };

  it("hit-tests read-only SYSTEM_LEVEL lines and zones without mutation", () => {
    const levels: KeyLevel[] = [
      {
        level_id: "lvl-1",
        instrument_id: "BTC-USDT-PERP",
        kind: "SWING_HIGH",
        category: "STRUCTURE",
        timeframe: "1m",
        price: "68500.0",
        strength: 0.9,
        status: "ACTIVE",
        ts_available: 1_700_000_000_000,
        formed_ts: 1_700_000_000_000,
        touch_count: 2,
        volume_weight: "100",
      },
      {
        level_id: "lvl-zone",
        instrument_id: "BTC-USDT-PERP",
        kind: "LIQUIDATION_ZONE",
        category: "LIQUIDATION",
        timeframe: "1m",
        price_low: "67400.0",
        price_high: "67600.0",
        strength: 0.7,
        status: "ACTIVE",
        ts_available: 1_700_000_000_000,
        formed_ts: 1_700_000_000_000,
        touch_count: 1,
        volume_weight: "50",
      },
    ];

    const ySwing = coords.priceToY(68500);
    expect(hitTestSystemLevel(levels, ySwing + 3, coords, scale, bounds)?.level_id).toBe("lvl-1");

    const yZoneMid = coords.priceToY(67500);
    expect(hitTestSystemLevel(levels, yZoneMid, coords, scale, bounds)?.level_id).toBe(
      "lvl-zone",
    );
    expect(hitTestSystemLevel(levels, coords.priceToY(68000), coords, scale, bounds)).toBeNull();
    // Level objects remain unmodified (read-only).
    expect(levels[0].price).toBe("68500.0");
  });

  it("hit-tests read-only SYSTEM_STRUCTURE across all 4 geometry primitives (linear & log)", () => {
    const t0 = 1_700_000_000_000;
    const t1 = t0 + 100_000;
    const structures: StructureFeature[] = [
      {
        structure_id: "st_line",
        instrument_id: "BTC-USDT-PERP",
        detector_id: "swing@1",
        family: "LEVEL",
        category: "STRUCTURE",
        kind: "SWING_HIGH",
        timeframe: "1m",
        geometry: { kind: "HORIZONTAL_LINE", price: "68800" },
        strength: 0.9,
        status: "ACTIVE",
        ts_available: t0,
        formed_ts: t0,
        analysis_window: { from_ts: t0, to_ts: t1 },
      },
      {
        structure_id: "st_zone",
        instrument_id: "BTC-USDT-PERP",
        detector_id: "fvg@1",
        family: "ZONE",
        category: "GAP",
        kind: "FVG_BULL",
        timeframe: "1m",
        geometry: {
          kind: "HORIZONTAL_ZONE",
          price_low: "67300",
          price_high: "67500",
          start_ts: t0,
          end_ts: t1,
        },
        strength: 0.8,
        status: "ACTIVE",
        ts_available: t0,
        formed_ts: t0,
        analysis_window: { from_ts: t0, to_ts: t1 },
      },
      {
        structure_id: "st_slope_lin",
        instrument_id: "BTC-USDT-PERP",
        detector_id: "pivot_trendline@1",
        family: "TRENDLINE",
        category: "TREND",
        kind: "TRENDLINE_SUPPORT",
        timeframe: "1m",
        geometry: {
          kind: "SLOPE_LINE",
          anchor_start: { ts: t0, price: "67800" },
          anchor_end: { ts: t1, price: "68200" },
          scale: "LINEAR",
          extend_right: true,
        },
        strength: 0.85,
        status: "ACTIVE",
        ts_available: t1,
        formed_ts: t0,
        analysis_window: { from_ts: t0, to_ts: t1 },
      },
      {
        structure_id: "st_chan",
        instrument_id: "BTC-USDT-PERP",
        detector_id: "channel@1",
        family: "CHANNEL",
        category: "TREND",
        kind: "CHANNEL_UP",
        timeframe: "1m",
        geometry: {
          kind: "CHANNEL",
          upper: {
            kind: "SLOPE_LINE",
            anchor_start: { ts: t0, price: "68450" },
            anchor_end: { ts: t1, price: "68650" },
            scale: "LOG",
            extend_right: false,
          },
          lower: {
            kind: "SLOPE_LINE",
            anchor_start: { ts: t0, price: "68250" },
            anchor_end: { ts: t1, price: "68450" },
            scale: "LOG",
            extend_right: false,
          },
        },
        strength: 0.75,
        status: "ACTIVE",
        ts_available: t1,
        formed_ts: t0,
        analysis_window: { from_ts: t0, to_ts: t1 },
      },
    ];

    expect(hitTestSystemStructure(structures, 50, coords.priceToY(68800) + 2, coords, scale, bounds)?.structure_id).toBe("st_line");
    expect(hitTestSystemStructure(structures, coords.timeToX(t0 + 50_000), coords.priceToY(67400), coords, scale, bounds)?.structure_id).toBe("st_zone");
    // Slope line extended right at t = t0 + 150_000 -> price = 68400
    expect(hitTestSystemStructure(structures, coords.timeToX(t0 + 150_000), coords.priceToY(68400) + 1, coords, scale, bounds)?.structure_id).toBe("st_slope_lin");
    // Channel interior mid at t0 + 50_000 -> approx 68450
    expect(hitTestSystemStructure(structures, coords.timeToX(t0 + 50_000), coords.priceToY(68450), coords, scale, bounds)?.structure_id).toBe("st_chan");
  });

  it("hit-tests read-only AGENT_ANNOTATION levels and zones", () => {
    const annotations: AgentAnnotationRecord[] = [
      {
        annotation_id: "ann-1",
        snapshot_id: "snap-1",
        instrument_id: "BTC-USDT-PERP",
        timeframe: "1m",
        session_id: "sess-1",
        round: 1,
        kind: "LEVEL",
        status: "ACTIVE",
        label: "Support",
        rationale: "Liquidity sweep",
        price_low: "68000.0",
        price_high: "68000.0",
        ts_anchor: 1_700_000_050_000,
        evidence_refs: [],
        created_ts: 1_700_000_050_000,
        updated_ts: 1_700_000_050_000,
      },
    ];
    const hit = hitTestAgentAnnotation(
      annotations,
      100,
      coords.priceToY(68000) + 2,
      "1m",
      coords,
      scale,
      bounds,
    );
    expect(hit?.annotation_id).toBe("ann-1");
  });

  it("emits authoritative backend IndicatorSeries preserving value: null gaps with zero JS math", () => {
    const series: IndicatorSeries = {
      name: "EMA",
      pane: "main",
      params: { period: 9 },
      points: [
        { timestamp: 1_000, values: { value: null } },
        { timestamp: 2_000, values: { value: "68123.45" } },
      ],
      summary: { latest: { value: "68123.45" }, warmup_bars: 1, valid_points: 1 },
    };
    const aligned = alignIndicatorChannelPoints(
      [
        { time: 1_000, open: 1, high: 2, low: 1, close: 2 },
        { time: 2_000, open: 2, high: 3, low: 2, close: 3 },
        { time: 3_000, open: 3, high: 4, low: 3, close: 4 },
      ],
      series,
      "value",
    );
    expect(aligned).toEqual([
      { time: 1_000, value: null },
      { time: 2_000, value: 68123.45 },
      { time: 3_000, value: null },
    ]);
  });

  it("registers floating drawing price-alert action and round-trips human drawings", async () => {
    ensureDrawingAlertActionRegistered();
    const registered = drawingActions();
    const alertAction = registered.find((d) => d.id === "talaria.create_price_alert");
    expect(alertAction).toBeDefined();
    expect(alertAction?.surfaces).toEqual(["toolbar", "settings"]);

    const record: DrawingRecord = {
      drawing_id: "dw-remote-1",
      instrument_id: "BTC-USDT-PERP",
      kind: "HORIZONTAL_LINE",
      visible_timeframes: ["1m"],
      points: [{ ts: 1_700_000_000_000, price: "68200.5" }],
      text: "Resistance",
      style: { color: "#4c8dff", line_width: 2, line_style: "solid", fill_opacity: 0.12 },
      version: 1,
      created_ts: 1_700_000_000_000,
      updated_ts: 1_700_000_000_000,
      deleted_at: null,
    };

    const serialized = contractDrawingToSerialized(record);
    expect(serialized.type).toBe("hline");
    expect(serialized.anchors[0]).toEqual({ time: 1_700_000_000_000, price: 68200.5 });

    const createdPayload = serializedDrawingToCreate(serialized, "BTC-USDT-PERP", "1m", 1);
    expect(createdPayload.kind).toBe("HORIZONTAL_LINE");
    expect(createdPayload.points[0].price).toBe("68200.5");

    // Verify extra Vela tool (from the 79-tool palette) round-trips cleanly via envelope.
    const gannEnvelope = serializedDrawingToCreate(
      { ...serialized, type: "gannbox" },
      "BTC-USDT-PERP",
      "1m",
      1,
    );
    expect(gannEnvelope.kind).toBe("TEXT_LABEL");
    const restoredGann = contractDrawingToSerialized({
      ...record,
      kind: "TEXT_LABEL",
      text: gannEnvelope.text ?? "",
      points: gannEnvelope.points,
    });
    expect(restoredGann.type).toBe("gannbox");

    // Verify debounced write sync on drawing:created using mock Vela chart.
    vi.useFakeTimers();
    const listeners = new Map<string, Array<(payload: unknown) => void>>();
    const mockChart = {
      on: (event: string, cb: (payload: unknown) => void) => {
        const list = listeners.get(event) ?? [];
        list.push(cb);
        listeners.set(event, list);
        return () => undefined;
      },
      drawings: {
        all: () => [serialized],
        fromJSON: vi.fn(),
        showToolbar: vi.fn(),
      },
      renderer: {
        set: vi.fn(),
      },
    } as never;

    const createDrawing = vi.fn().mockResolvedValue(record);
    const onChanged = vi.fn();
    const bridge = new VelaDrawingSyncBridge({
      chart: mockChart,
      instrumentId: "BTC-USDT-PERP",
      timeframe: "1m",
      pricePrecision: 1,
      onChanged,
      client: {
        drawings: vi.fn().mockResolvedValue({ drawings: [record] }),
        createDrawing,
        patchDrawing: vi.fn().mockResolvedValue(record),
        deleteDrawing: vi.fn().mockResolvedValue(record),
      },
    });

    // Remote fetch/load emits "load" so multi-pane listeners do not re-broadcast drawings-changed.
    await bridge.fetchAndLoad();
    expect(onChanged).toHaveBeenLastCalledWith([record], "load");

    for (const cb of listeners.get("drawing:created") ?? []) {
      cb({ id: serialized.id });
    }
    vi.advanceTimersByTime(250);
    await bridge.flush();
    expect(createDrawing).toHaveBeenCalledTimes(1);
    expect(onChanged).toHaveBeenLastCalledWith([record], "mutation");
    bridge.destroy();
    vi.useRealTimers();
  });
});
