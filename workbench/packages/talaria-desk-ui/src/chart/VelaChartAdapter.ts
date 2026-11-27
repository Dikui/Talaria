import {
  Vela,
  type IndicatorHandle,
  type ReplayBounds,
  type ReplayStartOptions,
  type ReplayState,
  type ReplayTickSource,
  type TradeExecution,
  type VelaEventMap,
} from "@luxalgo/vela";
import type {
  AgentAnnotationRecord,
  CompactBar,
  DraftRecord,
  DrawingRecord,
  IndicatorSeries,
  KeyLevel,
  KlineEvent,
  StructureFeature,
} from "@talaria/contracts";

import { compactBarToOHLCV, compactBarsToVelaSeries, timeframeToMs } from "../lib/chartData";
import type { Locale } from "../lib/i18n";
import { describeParams } from "../lib/indicatorData";
import type { Theme } from "../lib/theme";
import type {
  BarrierDragOverlay,
  BarrierDragProposal,
  ChartMountOptions,
  ChartReplayControl,
  ChartSurfaceAdapter,
  ExecutionBarrierItem,
  RenderBackendMode,
  ReplayDrawingFilter,
  ReplayEventName,
} from "./adapter";
import {
  BarrierDragController,
  type BarrierHoverState,
} from "./barrierDragController";
import {
  VelaDrawingSyncBridge,
  bindChartDrawingActions,
} from "./drawingBridge";
import {
  OrderGestureTracker,
  extractPositionGeometry,
} from "./orderGestureTracker";
import { VelaMarketFeed } from "./VelaMarketFeed";
import {
  TALARIA_INDICATOR_LANGUAGE,
  TALARIA_TRADE_MARKERS_LANGUAGE,
  TALARIA_TRADE_MARKERS_TYPE,
  clearAuthoritativeIndicatorSlot,
  clearChartTradeMarkers,
  createAuthoritativeIndicatorEngine,
  createTradeMarkersEngine,
  setAuthoritativeIndicatorLocale,
  setAuthoritativeIndicatorSlot,
  setChartTradeMarkers,
} from "./velaIndicators";
import {
  LAYER_AGENT_ANNOTATION,
  LAYER_EXECUTION_BARRIER,
  LAYER_STAGED_DRAFT,
  LAYER_SYSTEM_LEVEL,
  LAYER_SYSTEM_STRUCTURE,
  clearChartLayerState,
  ensureTalariaLayersRegistered,
  getLatestProjection,
  hitTestAgentAnnotation,
  hitTestStagedDraft,
  hitTestSystemLevel,
  hitTestSystemStructure,
  setChartLayerChannelData,
} from "./velaLayers";
import { webglBudgetManager } from "./webglBudget";

const INITIAL_DEPTH = 300;
const DEPTH_STEP = 300;
const LEFT_EDGE_BARS_THRESHOLD = 24;

const IDLE_REPLAY_STATE: ReplayState = {
  active: false,
  playing: false,
  cursorTime: null,
  remaining: 0,
  nextTime: null,
  intervalMs: 1000,
};

/**
 * Primary ChartSurfaceAdapter implementation backed by `@luxalgo/vela@0.10.17`
 * (Apache-2.0 headless core + `@luxalgo/vela/plugin` SDK).
 */
export class VelaChartAdapter implements ChartSurfaceAdapter {
  private chart: Vela | null = null;
  private feed: VelaMarketFeed | null = null;
  private drawingBridge: VelaDrawingSyncBridge | null = null;
  private orderGestureTracker: OrderGestureTracker | null = null;
  private barrierDragController: BarrierDragController | null = null;
  private unbindDrawingActions: (() => void) | null = null;
  private options: ChartMountOptions | null = null;
  private chartId = "";
  private instrumentId = "";
  private timeframe = "1m";
  private pricePrecision = 2;
  private currentDepth = INITIAL_DEPTH;
  private expandingHistory = false;
  private backendMode: RenderBackendMode = "auto";
  private locale: Locale = "zh";
  private readOnly = false;
  private replayActive = false;

  private systemLevels: KeyLevel[] = [];
  private systemHighlightId: string | null = null;
  private systemStructures: StructureFeature[] = [];
  private systemStructureHighlightId: string | null = null;
  private agentAnnotations: AgentAnnotationRecord[] = [];
  private agentHighlightId: string | null = null;
  private executionBarriers: ExecutionBarrierItem[] = [];
  private barrierDragOverlay: BarrierDragOverlay | null = null;
  private barrierHover: BarrierHoverState | null = null;
  private stagedDrafts: DraftRecord[] = [];
  private stagedDraftHighlightId: string | null = null;

  private indicatorHandles = new Map<string, { handle: IndicatorHandle; version: number; pane: "main" | "sub" }>();
  private tradeMarkersHandle: IndicatorHandle | null = null;
  private tradeMarkersVersion = 0;
  private unsubscribers: Array<() => void> = [];

  readonly replay: ChartReplayControl;

  constructor() {
    const self = this;
    this.replay = {
      start: async (opts: ReplayStartOptions) => {
        if (!self.chart) return;
        await self.chart.replay.start(opts);
      },
      step: () => self.chart?.replay.step() ?? false,
      stepUpdate: () => self.chart?.replay.stepUpdate() ?? false,
      play: (intervalMs?: number) => {
        self.chart?.replay.play(intervalMs);
      },
      pause: () => {
        self.chart?.replay.pause();
      },
      stop: () => {
        self.chart?.replay.stop();
      },
      setTicks: (source: ReplayTickSource | null) => {
        self.chart?.replay.setTicks(source);
      },
      get state(): ReplayState {
        return self.chart?.replay.state ?? IDLE_REPLAY_STATE;
      },
      get bounds(): ReplayBounds | null {
        return self.chart?.replay.bounds ?? null;
      },
      on: <K extends ReplayEventName>(
        event: K,
        handler: (payload: VelaEventMap[K]) => void,
      ) => {
        if (!self.chart) return () => undefined;
        return self.chart.on(event, handler);
      },
    };
  }

  mount(container: HTMLElement, options: ChartMountOptions): void {
    this.destroy();

    ensureTalariaLayersRegistered();

    this.options = options;
    this.chartId = options.chartId;
    this.instrumentId = options.instrumentId;
    this.timeframe = options.timeframe;
    this.pricePrecision = options.pricePrecision;
    this.locale = options.locale;
    this.readOnly = options.readOnly ?? false;
    this.currentDepth = INITIAL_DEPTH;

    this.backendMode = webglBudgetManager.registerChart(this.chartId);
    options.onBackendChange?.(this.backendMode);

    this.feed = new VelaMarketFeed({
      snapshotCutoff: options.snapshotCutoff ?? null,
      onSnapshot: options.onSnapshot,
      onStats: options.onStats,
      onError: options.onLoadError,
    });

    const chart = new Vela(
      container,
      {
        symbol: options.instrumentId,
        timeframe: options.timeframe,
        bars: this.currentDepth,
        live: (options.live ?? true) && (options.snapshotCutoff ?? null) === null,
        theme: options.theme,
        locale: options.locale,
        nativeBackend: this.backendMode,
        drawings: this.readOnly ? false : { toolbar: true },
        animations: { intro: false, liveBar: false },
      },
      {
        dataFeed: this.feed,
      },
    );
    this.chart = chart;

    chart.renderer.set("drawingsInteractive", !this.readOnly);
    // Suppress Vela's built-in naive Loss/Size label so Talaria's Rust sizing preview is authoritative (ADR-0010 / D4).
    chart.drawings.setToolDefaults({
      position: { props: { showLossSize: false } },
    });

    // Mount isolated read-only renderer layer channels (legend: false).
    chart.addNativeIndicator(LAYER_SYSTEM_LEVEL, { inputs: { chartId: this.chartId } });
    chart.addNativeIndicator(LAYER_SYSTEM_STRUCTURE, { inputs: { chartId: this.chartId } });
    chart.addNativeIndicator(LAYER_AGENT_ANNOTATION, { inputs: { chartId: this.chartId } });
    chart.addNativeIndicator(LAYER_EXECUTION_BARRIER, { inputs: { chartId: this.chartId } });
    chart.addNativeIndicator(LAYER_STAGED_DRAFT, { inputs: { chartId: this.chartId } });
    chart.registerEngine(TALARIA_INDICATOR_LANGUAGE, createAuthoritativeIndicatorEngine());
    chart.registerEngine(TALARIA_TRADE_MARKERS_LANGUAGE, createTradeMarkersEngine());
    this.tradeMarkersVersion = 0;
    this.tradeMarkersHandle = chart.addIndicator(TALARIA_TRADE_MARKERS_TYPE, {
      id: `talaria-trades-${this.chartId}`,
      language: TALARIA_TRADE_MARKERS_LANGUAGE,
      overlay: true,
      pane: "price",
      inputs: { chartId: this.chartId, version: 0 },
    });

    this.drawingBridge = new VelaDrawingSyncBridge({
      chart,
      instrumentId: this.instrumentId,
      timeframe: this.timeframe,
      pricePrecision: this.pricePrecision,
      readOnly: this.readOnly,
      onChanged: options.onDrawingsChanged,
      onSelected: options.onDrawingSelect,
      onError: options.onDrawingError,
    });

    this.orderGestureTracker = new OrderGestureTracker({
      chart,
      container,
      chartId: this.chartId,
      getPricePrecision: () => this.pricePrecision,
      onGestureChange: options.onOrderGestureChange,
    });

    this.barrierDragController = new BarrierDragController({
      container,
      getProjection: () => getLatestProjection(this.chartId),
      getBarriers: () => this.executionBarriers,
      getPricePrecision: () => this.pricePrecision,
      isToolActive: () =>
        chart.drawings.getTool() !== null || chart.drawings.getMode() !== null,
      isReadOnly: () => this.readOnly,
      isReplayActive: () => this.replayActive,
      onOverlayChange: (overlay) => {
        this.barrierDragOverlay = overlay;
        this.pushExecutionBarrierLayer();
        options.onBarrierOverlayChange?.(overlay);
      },
      onHoverChange: (hover) => {
        this.barrierHover = hover;
        this.pushExecutionBarrierLayer();
      },
      onDragEnd: (proposal) => {
        options.onBarrierDragEnd?.(proposal);
      },
      onDisabledAttempt: (ev) => {
        options.onBarrierDragDisabled?.(ev);
      },
      onPendingAckTimeout: (ov) => {
        options.onBarrierPendingAckTimeout?.(ov);
      },
    });

    if (options.onCreateAlertFromDrawing || options.onCreateDraftFromOrderGesture) {
      this.unbindDrawingActions = bindChartDrawingActions(chart, {
        onCreateAlert: options.onCreateAlertFromDrawing,
        onCreateDraft: options.onCreateDraftFromOrderGesture
          ? (drawing) => {
              const geom = extractPositionGeometry(drawing, this.pricePrecision, "placed");
              if (!geom) return;
              options.onCreateDraftFromOrderGesture?.({
                drawingId: drawing.id,
                direction: geom.direction,
                entry: geom.entryStr,
                stop: geom.stopStr,
                target: geom.targetStr,
                validGeometry: geom.validGeometry,
              });
            }
          : undefined,
      });
    }

    this.unsubscribers.push(
      chart.on("replay:start", () => {
        this.replayActive = true;
        this.clearOrderGesture();
        this.barrierDragController?.cancelPendingConfirm();
        this.setStagedDrafts([]);
        this.pushExecutionBarrierLayer();
      }),
      chart.on("replay:end", () => {
        this.replayActive = false;
        this.pushExecutionBarrierLayer();
      }),
      chart.on("drawing:tool", ({ type }) => {
        options.onDrawingToolChange?.(type);
      }),
      chart.renderer.onCrosshairMove((event) => {
        options.onCrosshairMove?.({
          time: event.time,
          price: event.paneKind === "price" ? event.price : null,
        });
      }),
      chart.on("viewport:changed", ({ from }) => {
        void this.maybeExpandLeftEdge(from);
      }),
      chart.renderer.onClick((event) => {
        if (event.paneKind && event.paneKind !== "price") return;
        if (chart.drawings.getTool() !== null) return;
        const proj = getLatestProjection(this.chartId);
        if (!proj || event.price === null) return;
        const cursorY = proj.coords.priceToY(event.price, proj.scale, proj.bounds);
        const cursorX =
          event.time !== null ? proj.coords.timeToX(event.time) : proj.coords.width / 2;

        if (!this.replayActive && this.stagedDrafts.length > 0) {
          const hitDraft = hitTestStagedDraft(
            this.stagedDrafts,
            cursorY,
            proj.coords,
            proj.scale,
            proj.bounds,
          );
          if (hitDraft) {
            options.onSelectStagedDraft?.(hitDraft);
            return;
          }
        }

        const hitAnnotation = hitTestAgentAnnotation(
          this.agentAnnotations,
          cursorX,
          cursorY,
          this.timeframe,
          proj.coords,
          proj.scale,
          proj.bounds,
        );
        if (hitAnnotation) {
          options.onSelectAnnotation?.(hitAnnotation);
          return;
        }

        const hitStructure = hitTestSystemStructure(
          this.systemStructures,
          cursorX,
          cursorY,
          proj.coords,
          proj.scale,
          proj.bounds,
        );
        if (hitStructure) {
          options.onSelectStructure?.(hitStructure);
          return;
        }

        const hitLevel = hitTestSystemLevel(
          this.systemLevels,
          cursorY,
          proj.coords,
          proj.scale,
          proj.bounds,
        );
        if (hitLevel) {
          options.onSelectKeyLevel?.(hitLevel);
        }
      }),
    );

    void this.drawingBridge.fetchAndLoad();
  }

  private async maybeExpandLeftEdge(visibleFromMs: number): Promise<void> {
    if (!this.chart || !this.feed || this.expandingHistory) return;
    if (!this.feed.canExpandBackward()) return;
    const oldest = this.feed.oldestLoadedTimestamp();
    if (oldest === null) return;
    const thresholdMs = LEFT_EDGE_BARS_THRESHOLD * timeframeToMs(this.timeframe);
    if (visibleFromMs > oldest + thresholdMs) return;

    this.expandingHistory = true;
    this.options?.onViewportLeftEdge?.(oldest);
    try {
      this.currentDepth += DEPTH_STEP;
      await this.chart.setMarket({ bars: this.currentDepth });
    } finally {
      this.expandingHistory = false;
    }
  }

  async setMarket(params: {
    instrumentId: string;
    timeframe: string;
    pricePrecision: number;
    volumePrecision: number;
    live?: boolean;
    snapshotCutoff?: number | null;
  }): Promise<void> {
    if (!this.chart || !this.feed) return;
    // Clear ephemeral order gesture on BOTH symbol and timeframe switch (Ticket 06 §1.3).
    this.clearOrderGesture();
    this.barrierDragController?.cancelPendingConfirm();
    this.barrierDragController?.resolvePendingAck();
    this.instrumentId = params.instrumentId;
    this.timeframe = params.timeframe;
    this.pricePrecision = params.pricePrecision;
    this.currentDepth = INITIAL_DEPTH;
    this.feed.setSnapshotCutoff(params.snapshotCutoff ?? null);
    this.drawingBridge?.setContext(
      params.instrumentId,
      params.timeframe,
      params.pricePrecision,
    );

    await this.chart.setMarket({
      symbol: params.instrumentId,
      timeframe: params.timeframe,
      bars: this.currentDepth,
    });
    await this.drawingBridge?.fetchAndLoad();
  }

  setBars(bars: CompactBar[]): void {
    if (!this.chart) return;
    const series = compactBarsToVelaSeries(bars);
    this.options?.onStats?.({ loaded: series.bars.length, gaps: series.gapCount });
    void this.chart.setMarket({
      symbol: this.instrumentId,
      timeframe: this.timeframe,
      data: series.bars,
    });
  }

  updateLiveBar(event: KlineEvent): void {
    if (!this.chart) return;
    if (event.bar.status === "FEED_GAP") return;
    const point = compactBarToOHLCV(event.bar);
    if (!point) return;
    // Live bars flow automatically via `VelaMarketFeed.subscribe`.
    // When pushed imperatively (e.g. external offline data mode), re-sync if offline.
    if (this.chart.market.offline) {
      const current = this.chart.inspect();
      void current;
    }
  }

  setIndicators(seriesList: IndicatorSeries[]): void {
    if (!this.chart) return;
    const nextKeys = new Set<string>();

    for (const series of seriesList) {
      const key = `${series.name}:${series.pane}`;
      nextKeys.add(key);
      const slotKey = `${this.chartId}:${key}`;
      const existing = this.indicatorHandles.get(key);

      if (existing && existing.pane === series.pane) {
        const nextVersion = existing.version + 1;
        setAuthoritativeIndicatorSlot(slotKey, series, nextVersion, this.locale);
        existing.version = nextVersion;
        existing.handle.setInputs({ slotKey, version: nextVersion });
      } else {
        if (existing) {
          existing.handle.remove();
          clearAuthoritativeIndicatorSlot(slotKey);
        }
        const version = 1;
        setAuthoritativeIndicatorSlot(slotKey, series, version, this.locale);
        const handle = this.chart.addIndicator(slotKey, {
          language: TALARIA_INDICATOR_LANGUAGE,
          overlay: series.pane === "main",
          pane: series.pane === "main" ? "price" : "new",
          title: describeParams(series, this.locale),
          inputs: { slotKey, version },
        });
        this.indicatorHandles.set(key, { handle, version, pane: series.pane });
      }
    }

    for (const [key, entry] of [...this.indicatorHandles.entries()]) {
      if (!nextKeys.has(key)) {
        entry.handle.remove();
        clearAuthoritativeIndicatorSlot(`${this.chartId}:${key}`);
        this.indicatorHandles.delete(key);
      }
    }
  }

  setTradeMarkers(trades: TradeExecution[]): void {
    if (!this.chart || !this.chartId) return;
    this.tradeMarkersVersion += 1;
    setChartTradeMarkers(this.chartId, trades, this.tradeMarkersVersion);
    this.tradeMarkersHandle?.setInputs({
      chartId: this.chartId,
      version: this.tradeMarkersVersion,
    });
  }

  setSystemLevels(
    levels: KeyLevel[],
    opts?: { precision?: number; highlightId?: string | null },
  ): void {
    this.systemLevels = levels;
    if (opts?.highlightId !== undefined) {
      this.systemHighlightId = opts.highlightId;
    }
    setChartLayerChannelData(this.chartId, LAYER_SYSTEM_LEVEL, {
      chartId: this.chartId,
      levels,
      precision: opts?.precision ?? this.pricePrecision,
      highlightId: this.systemHighlightId,
    });
  }

  setSystemStructures(
    structures: StructureFeature[],
    opts?: { precision?: number; highlightId?: string | null },
  ): void {
    this.systemStructures = structures;
    if (opts?.highlightId !== undefined) {
      this.systemStructureHighlightId = opts.highlightId;
    }
    setChartLayerChannelData(this.chartId, LAYER_SYSTEM_STRUCTURE, {
      chartId: this.chartId,
      structures,
      precision: opts?.precision ?? this.pricePrecision,
      highlightId: this.systemStructureHighlightId,
    });
  }

  setAgentAnnotations(
    annotations: AgentAnnotationRecord[],
    opts?: { timeframe?: string; highlightId?: string | null },
  ): void {
    this.agentAnnotations = annotations;
    if (opts?.highlightId !== undefined) {
      this.agentHighlightId = opts.highlightId;
    }
    setChartLayerChannelData(this.chartId, LAYER_AGENT_ANNOTATION, {
      chartId: this.chartId,
      annotations,
      timeframe: opts?.timeframe ?? this.timeframe,
      highlightId: this.agentHighlightId,
    });
  }

  private pushExecutionBarrierLayer(): void {
    if (!this.chartId) return;
    setChartLayerChannelData(this.chartId, LAYER_EXECUTION_BARRIER, {
      chartId: this.chartId,
      barriers: this.executionBarriers,
      dragOverlay: this.barrierDragOverlay,
      hover: this.barrierHover,
      readOnly: this.readOnly,
      replayActive: this.replayActive,
    });
  }

  setExecutionBarriers(barriers: ExecutionBarrierItem[]): void {
    this.executionBarriers = barriers;
    this.pushExecutionBarrierLayer();
  }

  cancelBarrierDragConfirm(): void {
    this.barrierDragController?.cancelPendingConfirm();
  }

  markBarrierDragPendingAck(commandId: string, proposal: BarrierDragProposal): void {
    this.barrierDragController?.markPendingAck(commandId, proposal);
  }

  resolveBarrierDragPendingAck(commandId?: string): void {
    this.barrierDragController?.resolvePendingAck(commandId);
  }

  setStagedDrafts(
    drafts: DraftRecord[],
    opts?: { precision?: number; highlightId?: string | null },
  ): void {
    this.stagedDrafts = drafts;
    if (opts?.highlightId !== undefined) {
      this.stagedDraftHighlightId = opts.highlightId;
    }
    setChartLayerChannelData(this.chartId, LAYER_STAGED_DRAFT, {
      chartId: this.chartId,
      drafts: this.stagedDrafts,
      precision: opts?.precision ?? this.pricePrecision,
      highlightId: this.stagedDraftHighlightId,
    });
  }

  loadHumanDrawings(drawings: DrawingRecord[]): void {
    this.drawingBridge?.loadFromRecords(drawings);
  }

  setReplayDrawingFilter(filter: ReplayDrawingFilter): void {
    this.drawingBridge?.setReplayFilter(filter);
  }

  setDrawingTool(tool: string | null): void {
    if (!this.chart || this.readOnly || this.replayActive) return;
    this.chart.drawings.setTool((tool as never) ?? null);
  }

  armOrderGesture(direction: "LONG" | "SHORT" | null): void {
    if (!this.chart || this.readOnly || this.replayActive) return;
    this.orderGestureTracker?.armOrderTool(direction);
  }

  clearOrderGesture(): void {
    if (this.chart?.drawings.getTool() === "position") {
      this.chart.drawings.setTool(null);
    }
    this.drawingBridge?.clearEphemeralDrawings();
    this.orderGestureTracker?.clear();
  }

  selectHumanDrawing(drawingId: string | null): void {
    if (!this.chart) return;
    this.chart.drawings.select(drawingId);
  }

  exportDrawingsDocument() {
    if (!this.chart) return null;
    return this.drawingBridge?.exportHumanDrawingsDocument() ?? this.chart.drawings.toJSON();
  }

  setTheme(theme: Theme): void {
    this.chart?.setTheme(theme);
  }

  setLocale(locale: Locale): void {
    this.locale = locale;
    this.chart?.setLocale(locale === "zh" ? "zh-Hans" : "en");
    for (const [key, entry] of this.indicatorHandles.entries()) {
      const slotKey = `${this.chartId}:${key}`;
      if (setAuthoritativeIndicatorLocale(slotKey, locale)) {
        entry.version += 1;
        entry.handle.setInputs({ slotKey, version: entry.version });
      }
    }
  }

  setReadOnly(readOnly: boolean): void {
    this.readOnly = readOnly;
    this.drawingBridge?.setReadOnly(readOnly);
    this.pushExecutionBarrierLayer();
  }

  setBackendMode(backend: RenderBackendMode): void {
    this.backendMode = backend;
    this.options?.onBackendChange?.(backend);
  }

  setExternalCrosshair(time: number | null, price?: number | null): void {
    if (!this.chart?.renderer.supportsExternalCrosshair) return;
    this.chart.renderer.setExternalCrosshair(time, price);
  }

  setVisibleRange(range: { from: number; to: number }): void {
    this.chart?.setVisibleRange(range);
  }

  getVisibleRange(): { from: number; to: number } | null {
    return this.chart?.getVisibleRange() ?? null;
  }

  getOldestLoadedBarTime(): number | null {
    return this.chart?.replay.bounds?.first ?? this.feed?.oldestLoadedTimestamp() ?? null;
  }

  async refresh(): Promise<void> {
    if (!this.chart) return;
    await this.chart.setMarket({
      symbol: this.instrumentId,
      timeframe: this.timeframe,
      bars: this.currentDepth,
      force: true,
    });
    await this.drawingBridge?.fetchAndLoad();
  }

  resize(): void {
    this.chart?.resize();
  }

  destroy(): void {
    for (const unsub of this.unsubscribers) {
      unsub();
    }
    this.unsubscribers = [];

    for (const [key, entry] of this.indicatorHandles.entries()) {
      entry.handle.remove();
      clearAuthoritativeIndicatorSlot(`${this.chartId}:${key}`);
    }
    this.indicatorHandles.clear();

    this.tradeMarkersHandle?.remove();
    this.tradeMarkersHandle = null;
    if (this.chartId) {
      clearChartTradeMarkers(this.chartId);
    }
    this.replayActive = false;

    this.unbindDrawingActions?.();
    this.unbindDrawingActions = null;

    this.orderGestureTracker?.destroy();
    this.orderGestureTracker = null;

    this.barrierDragController?.destroy();
    this.barrierDragController = null;
    this.barrierDragOverlay = null;
    this.barrierHover = null;

    this.drawingBridge?.destroy();
    this.drawingBridge = null;

    if (this.chartId) {
      clearChartLayerState(this.chartId);
      webglBudgetManager.unregisterChart(this.chartId);
    }

    this.chart?.destroy();
    this.chart = null;

    this.feed?.destroy();
    this.feed = null;
    this.options = null;
  }
}
