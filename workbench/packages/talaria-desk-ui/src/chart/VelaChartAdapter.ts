import { Vela, type IndicatorHandle } from "@luxalgo/vela";
import type {
  AgentAnnotationRecord,
  CompactBar,
  DrawingRecord,
  IndicatorSeries,
  KeyLevel,
  KlineEvent,
} from "@talaria/contracts";

import { compactBarToOHLCV, compactBarsToVelaSeries, timeframeToMs } from "../lib/chartData";
import type { Locale } from "../lib/i18n";
import type { Theme } from "../lib/theme";
import type {
  ChartMountOptions,
  ChartSurfaceAdapter,
  ExecutionBarrierItem,
  RenderBackendMode,
} from "./adapter";
import {
  VelaDrawingSyncBridge,
  bindChartDrawingAlertHandler,
} from "./drawingBridge";
import { VelaMarketFeed } from "./VelaMarketFeed";
import {
  TALARIA_OVERLAY_INDICATOR_TYPE,
  TALARIA_STUDY_INDICATOR_TYPE,
  clearAuthoritativeIndicatorSlot,
  ensureTalariaIndicatorsRegistered,
  setAuthoritativeIndicatorSlot,
} from "./velaIndicators";
import {
  LAYER_AGENT_ANNOTATION,
  LAYER_EXECUTION_BARRIER,
  LAYER_SYSTEM_LEVEL,
  clearChartLayerState,
  ensureTalariaLayersRegistered,
  getLatestProjection,
  hitTestAgentAnnotation,
  hitTestSystemLevel,
  setChartLayerChannelData,
} from "./velaLayers";
import { webglBudgetManager } from "./webglBudget";

const INITIAL_DEPTH = 300;
const DEPTH_STEP = 300;
const LEFT_EDGE_BARS_THRESHOLD = 24;

/**
 * Primary ChartSurfaceAdapter implementation backed by `@luxalgo/vela@0.10.17`
 * (Apache-2.0 headless core + `@luxalgo/vela/plugin` SDK).
 */
export class VelaChartAdapter implements ChartSurfaceAdapter {
  private chart: Vela | null = null;
  private feed: VelaMarketFeed | null = null;
  private drawingBridge: VelaDrawingSyncBridge | null = null;
  private unbindAlertAction: (() => void) | null = null;
  private options: ChartMountOptions | null = null;
  private chartId = "";
  private instrumentId = "";
  private timeframe = "1m";
  private pricePrecision = 2;
  private currentDepth = INITIAL_DEPTH;
  private expandingHistory = false;
  private backendMode: RenderBackendMode = "auto";
  private readOnly = false;

  private systemLevels: KeyLevel[] = [];
  private systemHighlightId: string | null = null;
  private agentAnnotations: AgentAnnotationRecord[] = [];
  private agentHighlightId: string | null = null;
  private executionBarriers: ExecutionBarrierItem[] = [];

  private indicatorHandles = new Map<string, { handle: IndicatorHandle; version: number; pane: "main" | "sub" }>();
  private unsubscribers: Array<() => void> = [];

  mount(container: HTMLElement, options: ChartMountOptions): void {
    this.destroy();

    ensureTalariaIndicatorsRegistered();
    ensureTalariaLayersRegistered();

    this.options = options;
    this.chartId = options.chartId;
    this.instrumentId = options.instrumentId;
    this.timeframe = options.timeframe;
    this.pricePrecision = options.pricePrecision;
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

    // Mount isolated read-only renderer layer channels (legend: false).
    chart.addNativeIndicator(LAYER_SYSTEM_LEVEL, { inputs: { chartId: this.chartId } });
    chart.addNativeIndicator(LAYER_AGENT_ANNOTATION, { inputs: { chartId: this.chartId } });
    chart.addNativeIndicator(LAYER_EXECUTION_BARRIER, { inputs: { chartId: this.chartId } });

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

    if (options.onCreateAlertFromDrawing) {
      this.unbindAlertAction = bindChartDrawingAlertHandler(
        chart,
        options.onCreateAlertFromDrawing,
      );
    }

    this.unsubscribers.push(
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
        setAuthoritativeIndicatorSlot(slotKey, series, nextVersion);
        existing.version = nextVersion;
        existing.handle.setInputs({ slotKey, version: nextVersion });
      } else {
        if (existing) {
          existing.handle.remove();
          clearAuthoritativeIndicatorSlot(slotKey);
        }
        const version = 1;
        setAuthoritativeIndicatorSlot(slotKey, series, version);
        const type =
          series.pane === "main"
            ? TALARIA_OVERLAY_INDICATOR_TYPE
            : TALARIA_STUDY_INDICATOR_TYPE;
        const handle = this.chart.addNativeIndicator(type, {
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

  setExecutionBarriers(barriers: ExecutionBarrierItem[]): void {
    this.executionBarriers = barriers;
    setChartLayerChannelData(this.chartId, LAYER_EXECUTION_BARRIER, {
      chartId: this.chartId,
      barriers: this.executionBarriers,
    });
  }

  loadHumanDrawings(drawings: DrawingRecord[]): void {
    this.drawingBridge?.loadFromRecords(drawings);
  }

  setDrawingTool(tool: string | null): void {
    if (!this.chart || this.readOnly) return;
    this.chart.drawings.setTool((tool as never) ?? null);
  }

  selectHumanDrawing(drawingId: string | null): void {
    if (!this.chart) return;
    this.chart.drawings.select(drawingId);
  }

  exportDrawingsDocument() {
    return this.chart ? this.chart.drawings.toJSON() : null;
  }

  setTheme(theme: Theme): void {
    this.chart?.setTheme(theme);
  }

  setLocale(locale: Locale): void {
    this.chart?.setLocale(locale === "zh" ? "zh-Hans" : "en");
  }

  setReadOnly(readOnly: boolean): void {
    this.readOnly = readOnly;
    this.drawingBridge?.setReadOnly(readOnly);
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

    this.unbindAlertAction?.();
    this.unbindAlertAction = null;

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
