import { describe, expect, it, vi } from "vitest";
import { act, render, renderHook, screen } from "@testing-library/react";
import type {
  AgentAnnotationRecord,
  DrawingRecord,
  IndicatorSeries,
  KeyLevelSet,
} from "@talaria/contracts";

import type { ChartSurfaceAdapter, ReplayEventName } from "../../chart/adapter";
import { VelaDrawingSyncBridge, filterReplayDrawings } from "../../chart/drawingBridge";
import { api } from "../../lib/api";
import { AlertPanel } from "../alerts/AlertPanel";
import type { AlertsController } from "../alerts/useAlerts";
import { defaultIndicatorSettings, INDICATOR_DEFAULTS } from "../indicators/config";
import { ReplayToolbar } from "./ReplayToolbar";
import { filterReplayAnnotations } from "./filters";
import {
  computeDeepenIndicatorLimit,
  needsIndicatorDeepenRefetch,
} from "./indicatorBackfill";
import { KeyLevelAsOfCache, shouldFetchReplayKeyLevels } from "./keyLevelCache";
import {
  closedBarCutoff,
  computeNextCursorTime,
  filterClosedBars,
  isBarClosedAt,
} from "./sync";
import { createReplayTickSource } from "./tickSource";
import { fetchReplayKeyLevelsIfNeeded, useChartReplay } from "./useChartReplay";

describe("Track A: Multi-TF replay sync, key-level throttling, indicator backfill & drawing isolation", () => {
  it("enforces strict closed-bar alignment (bar.time + tf_ms <= cursorTime) across 1m/15m/4h", () => {
    const tf1m = 60_000;
    const tf15m = 900_000;
    const tf4h = 14_400_000;

    // 14:00:00Z + 14m = 14:14:00Z closed at 14:15:00Z
    const t0 = Date.UTC(2026, 10, 25, 12, 0, 0);
    const cursorBefore15mClose = t0 + 2 * tf4h - 1 * tf1m; // 19:59:00? Actually 12:00 + 8h - 1m = 19:59
    const bars4h = [
      { time: t0, close: 100 }, // closes at t0 + 4h = 16:00
      { time: t0 + tf4h, close: 101 }, // closes at t0 + 8h = 20:00
    ];
    const bars15m = [
      { time: t0 + tf4h, close: 101 }, // closes at 16:15
      { time: t0 + tf4h + 14 * tf15m, close: 102 }, // closes at 19:45
      { time: t0 + tf4h + 15 * tf15m, close: 103 }, // closes at 20:00 (NOT closed at 19:59!)
    ];

    // At 19:59:00, 16:00 4h bar is STILL OPEN (16:00 + 4h = 20:00 > 19:59)
    expect(isBarClosedAt(t0 + tf4h, tf4h, cursorBefore15mClose)).toBe(false);
    expect(filterClosedBars(bars4h, tf4h, cursorBefore15mClose).map((b) => b.time)).toEqual([t0]);
    expect(filterClosedBars(bars15m, tf15m, cursorBefore15mClose).map((b) => b.time)).toEqual([
      t0 + tf4h,
      t0 + tf4h + 14 * tf15m,
    ]);
    expect(closedBarCutoff(cursorBefore15mClose, tf4h)).toBe(cursorBefore15mClose - tf4h);

    // Advance by Math.min(...nextClose) across 1m, 15m, 4h -> hits 20:00:00
    const nextCursor = computeNextCursorTime(
      [
        { paneId: "1m", tfMs: tf1m, nextTime: t0 + 2 * tf4h - tf1m }, // closes at 20:00
        { paneId: "15m", tfMs: tf15m, nextTime: t0 + tf4h + 15 * tf15m }, // closes at 20:00
        { paneId: "4h", tfMs: tf4h, nextTime: t0 + tf4h }, // closes at 20:00
      ],
      cursorBefore15mClose,
    );
    expect(nextCursor).toBe(t0 + 2 * tf4h);
    expect(filterClosedBars(bars4h, tf4h, nextCursor!).map((b) => b.time)).toEqual([
      t0,
      t0 + tf4h,
    ]);
  });

  it("pauses key-level recomputation during play and caches by (instrument, timeframe, as_of)", async () => {
    expect(shouldFetchReplayKeyLevels("tick", false)).toBe(false);
    expect(shouldFetchReplayKeyLevels("step", true)).toBe(false);
    expect(shouldFetchReplayKeyLevels("pause", false)).toBe(true);
    expect(shouldFetchReplayKeyLevels("step", false)).toBe(true);
    expect(shouldFetchReplayKeyLevels("seek", false)).toBe(true);

    const cache = new KeyLevelAsOfCache(16);
    const fetchKeyLevelsAt = vi.fn(
      async ({
        instrument,
        timeframe,
        asOf,
      }: {
        instrument: string;
        timeframe: string;
        asOf: number;
      }): Promise<KeyLevelSet> => ({
        snapshot_id: "at",
        instrument_id: instrument,
        timeframe,
        cutoff_ts: asOf,
        as_of_ts: asOf,
        levels: [],
        notes: [],
      }),
    );

    // While playing: throttled (0 calls)
    const duringPlay = await fetchReplayKeyLevelsIfNeeded({
      trigger: "step",
      isPlaying: true,
      instrumentId: "BTC-USDT-PERP",
      timeframe: "1m",
      asOf: 1_700_000_060_000,
      cache,
      fetchKeyLevelsAt,
    });
    expect(duringPlay).toBeNull();
    expect(fetchKeyLevelsAt).toHaveBeenCalledTimes(0);

    // On pause: fetches once and populates cache
    const onPause = await fetchReplayKeyLevelsIfNeeded({
      trigger: "pause",
      isPlaying: false,
      instrumentId: "BTC-USDT-PERP",
      timeframe: "1m",
      asOf: 1_700_000_060_000,
      cache,
      fetchKeyLevelsAt,
    });
    expect(onPause?.as_of_ts).toBe(1_700_000_060_000);
    expect(fetchKeyLevelsAt).toHaveBeenCalledTimes(1);

    // Second seek to same (instrument, timeframe, as_of): cache hit (0 extra calls)
    const cachedAgain = await fetchReplayKeyLevelsIfNeeded({
      trigger: "seek",
      isPlaying: false,
      instrumentId: "BTC-USDT-PERP",
      timeframe: "1m",
      asOf: 1_700_000_060_000,
      cache,
      fetchKeyLevelsAt,
    });
    expect(cachedAgain?.as_of_ts).toBe(1_700_000_060_000);
    expect(fetchKeyLevelsAt).toHaveBeenCalledTimes(1);
  });

  it("refetches GET /api/indicators?as_of=... once when replay start({from}) deepens history", async () => {
    const cachedSeries: IndicatorSeries[] = [
      {
        name: "EMA",
        pane: "main",
        params: { period: 9 },
        points: [
          { timestamp: 1_700_000_600_000, values: { value: "50000" } },
          { timestamp: 1_700_000_660_000, values: { value: "50010" } },
        ],
        summary: { latest: { value: "50010" }, warmup_bars: 0, valid_points: 2 },
      },
    ];

    expect(needsIndicatorDeepenRefetch(1_700_000_600_000, cachedSeries)).toBe(false);
    expect(needsIndicatorDeepenRefetch(1_700_000_000_000, cachedSeries)).toBe(true);
    expect(computeDeepenIndicatorLimit(1_700_000_000_000, 1_700_000_660_000, 60_000)).toBe(300);

    const replayHandlers = new Map<ReplayEventName, Array<(payload: never) => void>>();
    const setIndicatorsSpy = vi.fn();
    let currentBounds = { first: 1_700_000_000_000, last: 1_700_000_660_000 };

    const mockAdapter = {
      replay: {
        start: vi.fn(async ({ from }: { from: number }) => {
          const handlers = replayHandlers.get("replay:start") ?? [];
          for (const cb of handlers) {
            cb({ cursorTime: from, remaining: 5 } as never);
          }
        }),
        step: vi.fn(() => true),
        stepUpdate: vi.fn(() => true),
        play: vi.fn(),
        pause: vi.fn(),
        stop: vi.fn(),
        setTicks: vi.fn(),
        state: {
          active: true,
          playing: false,
          cursorTime: 1_700_000_300_000,
          remaining: 5,
          nextTime: 1_700_000_360_000,
          intervalMs: 1000,
        },
        get bounds() {
          return currentBounds;
        },
        on: (event: ReplayEventName, handler: (payload: never) => void) => {
          const list = replayHandlers.get(event) ?? [];
          list.push(handler);
          replayHandlers.set(event, list);
          return () => undefined;
        },
      },
      setIndicators: setIndicatorsSpy,
      setReplayDrawingFilter: vi.fn(),
      getOldestLoadedBarTime: () => currentBounds.first,
    } as unknown as ChartSurfaceAdapter;

    const backfilledSeries: IndicatorSeries[] = [
      {
        name: "EMA",
        pane: "main",
        params: { period: 9 },
        points: [
          { timestamp: 1_700_000_000_000, values: { value: "49900" } },
          { timestamp: 1_700_000_600_000, values: { value: "50000" } },
        ],
        summary: { latest: { value: "50000" }, warmup_bars: 0, valid_points: 2 },
      },
    ];

    const indicatorsAtSpy = vi.spyOn(api, "indicatorsAt").mockResolvedValue({
      snapshot_id: "live",
      instrument_id: "BTC-USDT-PERP",
      timeframe: "1m",
      series: backfilledSeries,
    });
    const keyLevelsAtSpy = vi.spyOn(api, "keyLevelsAt").mockResolvedValue({
      snapshot_id: "at",
      instrument_id: "BTC-USDT-PERP",
      timeframe: "1m",
      cutoff_ts: 1_700_000_360_000,
      as_of_ts: 1_700_000_360_000,
      levels: [],
      notes: [],
    });

    const onBackfilled = vi.fn();
    const { result } = renderHook(() =>
      useChartReplay({
        paneId: "chart-1m",
        instrumentId: "BTC-USDT-PERP",
        timeframe: "1m",
        group: "none",
        adapterRef: { current: mockAdapter },
        indicatorSettings: defaultIndicatorSettings(INDICATOR_DEFAULTS),
        indicatorSeries: cachedSeries,
        onIndicatorSeriesBackfilled: onBackfilled,
      }),
    );

    await act(async () => {
      await result.current.startAt(1_700_000_300_000);
    });

    expect(indicatorsAtSpy).toHaveBeenCalledTimes(1);
    expect(indicatorsAtSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        instrumentId: "BTC-USDT-PERP",
        timeframe: "1m",
        asOf: 1_700_000_360_000,
      }),
    );
    expect(setIndicatorsSpy).toHaveBeenCalledWith(backfilledSeries);
    expect(onBackfilled).toHaveBeenCalledWith(backfilledSeries);

    // Repeating start at the same deepened boundary does NOT refetch redundantly
    await act(async () => {
      await result.current.startAt(1_700_000_300_000);
    });
    expect(indicatorsAtSpy).toHaveBeenCalledTimes(1);

    indicatorsAtSpy.mockRestore();
    keyLevelsAtSpy.mockRestore();
  });

  it("filters HUMAN_DRAWING & AGENT_ANNOTATION by created_ts <= cursorTime and locks drawings read-only during replay", () => {
    const drawings: DrawingRecord[] = [
      {
        drawing_id: "d-past",
        instrument_id: "BTC-USDT-PERP",
        kind: "HORIZONTAL_LINE",
        visible_timeframes: ["1m"],
        points: [{ ts: 1000, price: "50000" }],
        text: "past",
        style: { color: "#4c8dff", line_width: 1, line_style: "solid", fill_opacity: 0.1 },
        version: 1,
        created_ts: 1000,
        updated_ts: 1000,
        deleted_at: null,
      },
      {
        drawing_id: "d-future",
        instrument_id: "BTC-USDT-PERP",
        kind: "HORIZONTAL_LINE",
        visible_timeframes: ["1m"],
        points: [{ ts: 5000, price: "51000" }],
        text: "future",
        style: { color: "#4c8dff", line_width: 1, line_style: "solid", fill_opacity: 0.1 },
        version: 1,
        created_ts: 5000,
        updated_ts: 5000,
        deleted_at: null,
      },
    ];

    expect(
      filterReplayDrawings(drawings, { active: true, cursorTime: 2000, showAll: false }).map(
        (d) => d.drawing_id,
      ),
    ).toEqual(["d-past"]);
    expect(
      filterReplayDrawings(drawings, { active: true, cursorTime: 2000, showAll: true }).map(
        (d) => d.drawing_id,
      ),
    ).toEqual(["d-past", "d-future"]);

    const annotations: AgentAnnotationRecord[] = [
      {
        annotation_id: "a-past",
        snapshot_id: "s1",
        instrument_id: "BTC-USDT-PERP",
        timeframe: "1m",
        session_id: "sess",
        round: 1,
        kind: "LEVEL",
        status: "ACTIVE",
        label: "Past",
        rationale: "",
        price_low: "50000",
        price_high: "50000",
        ts_anchor: 1000,
        evidence_refs: [],
        created_ts: 1000,
        updated_ts: 1000,
      },
      {
        annotation_id: "a-future",
        snapshot_id: "s1",
        instrument_id: "BTC-USDT-PERP",
        timeframe: "1m",
        session_id: "sess",
        round: 1,
        kind: "LEVEL",
        status: "ACTIVE",
        label: "Future",
        rationale: "",
        price_low: "51000",
        price_high: "51000",
        ts_anchor: 5000,
        evidence_refs: [],
        created_ts: 5000,
        updated_ts: 5000,
      },
    ];
    expect(
      filterReplayAnnotations(annotations, { active: true, cursorTime: 2000, showAll: false }).map(
        (a) => a.annotation_id,
      ),
    ).toEqual(["a-past"]);

    const listeners = new Map<string, Array<(payload: unknown) => void>>();
    const fromJSON = vi.fn();
    const showToolbar = vi.fn();
    const setRenderer = vi.fn();
    const createDrawing = vi.fn();
    const patchDrawing = vi.fn();
    const deleteDrawing = vi.fn();

    const mockChart = {
      on: (event: string, cb: (payload: unknown) => void) => {
        const list = listeners.get(event) ?? [];
        list.push(cb);
        listeners.set(event, list);
        return () => undefined;
      },
      drawings: {
        all: () => [],
        fromJSON,
        showToolbar,
      },
      renderer: {
        set: setRenderer,
      },
    } as never;

    const bridge = new VelaDrawingSyncBridge({
      chart: mockChart,
      instrumentId: "BTC-USDT-PERP",
      timeframe: "1m",
      pricePrecision: 2,
      client: {
        drawings: vi.fn(),
        createDrawing,
        patchDrawing,
        deleteDrawing,
      },
    });

    bridge.loadFromRecords(drawings);
    bridge.setReplayFilter({ active: true, cursorTime: 2000, showAll: false });

    expect(setRenderer).toHaveBeenLastCalledWith("drawingsInteractive", false);
    expect(showToolbar).toHaveBeenLastCalledWith(false);
    const lastDoc = fromJSON.mock.calls.at(-1)?.[0] as { drawings: Array<{ id: string }> };
    expect(lastDoc.drawings.map((d) => d.id)).toEqual(["d-past"]);

    // Even if Vela emits a drawing mutation event during replay, zero persistence writes occur
    for (const cb of listeners.get("drawing:created") ?? []) cb({ id: "d-past" });
    for (const cb of listeners.get("drawing:edited") ?? []) cb({ id: "d-past" });
    for (const cb of listeners.get("drawing:removed") ?? []) cb({ id: "d-past" });
    expect(createDrawing).not.toHaveBeenCalled();
    expect(patchDrawing).not.toHaveBeenCalled();
    expect(deleteDrawing).not.toHaveBeenCalled();

    // Toggling "显示全部画线" restores both without refetching
    bridge.setReplayFilter({ active: true, cursorTime: 2000, showAll: true });
    const restoredDoc = fromJSON.mock.calls.at(-1)?.[0] as { drawings: Array<{ id: string }> };
    expect(restoredDoc.drawings.map((d) => d.id)).toEqual(["d-past", "d-future"]);
    bridge.destroy();
  });

  it("handles empty /api/ticks fallback cleanly and tags AlertPanel as 实时 during replay", async () => {
    const emptySource = createReplayTickSource({
      instrumentId: "BTC-USDT-PERP",
      asOf: 1_700_000_060_000,
      fetchTicks: vi.fn().mockResolvedValue({
        instrument_id: "BTC-USDT-PERP",
        kind: "trades",
        start: 1_700_000_000_000,
        end: 1_700_000_060_000,
        aggregated: false,
        ticks: [],
      }),
    });
    const controller = new AbortController();
    const emptyResult = await emptySource(
      { time: 1_700_000_000_000, open: 100, high: 101, low: 99, close: 100.5, volume: 10 },
      { end: 1_700_000_060_000, signal: controller.signal },
    );
    expect(emptyResult).toEqual([]);

    render(
      <ReplayToolbar
        active
        playing={false}
        isMaster
        cursorTime={1_700_000_060_000}
        remaining={10}
        intervalMs={1000}
        bounds={{ first: 1_700_000_000_000, last: 1_700_000_600_000 }}
        tickReplayEnabled
        showAllDrawings={false}
        timeframe="1m"
        tfMs={60_000}
        onStartAt={() => undefined}
        onStepBar={() => undefined}
        onStepTick={() => undefined}
        onPlay={() => undefined}
        onPause={() => undefined}
        onStop={() => undefined}
        onChangeSpeed={() => undefined}
        onToggleTickReplay={() => undefined}
        onToggleShowAllDrawings={() => undefined}
      />,
    );
    expect(screen.getByTestId("replay-closed-alignment-label").textContent).toContain(
      "大周期按已收盘对齐",
    );

    const mockAlertsController: AlertsController = {
      alerts: [],
      triggers: [],
      unread: 0,
      toast: null,
      loading: false,
      error: null,
      soundEnabled: false,
      setSoundEnabled: vi.fn(),
      refresh: vi.fn(),
      create: vi.fn(),
      update: vi.fn(),
      remove: vi.fn(),
      confirm: vi.fn(),
      dismiss: vi.fn(),
      markSeen: vi.fn(),
      clearToast: vi.fn(),
      ingestTrigger: vi.fn(),
    };

    render(
      <AlertPanel
        controller={mockAlertsController}
        instruments={[]}
        drawings={[]}
        currentInstrument="BTC-USDT-PERP"
        currentTimeframe="1m"
        formSeed={null}
        replayActive
        onLocate={() => undefined}
      />,
    );
    expect(screen.getByTestId("alerts-live-badge").textContent).toBe("实时");
    expect(screen.getByTestId("alerts-replay-isolation-note")).toBeDefined();
  });
});
