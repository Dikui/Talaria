import type { DrawingsDocument, ReplayState, TradeExecution } from "@luxalgo/vela";
import type {
  AgentAnnotationRecord,
  CompactBar,
  DraftRecord,
  DrawingRecord,
  IndicatorSeries,
  KeyLevel,
  KlineEvent,
} from "@talaria/contracts";

import type { Locale } from "../lib/i18n";
import type { Theme } from "../lib/theme";
import type {
  BarrierDragProposal,
  ChartMountOptions,
  ChartReplayControl,
  ChartSurfaceAdapter,
  ExecutionBarrierItem,
  RenderBackendMode,
  ReplayDrawingFilter,
} from "./adapter";

const IDLE_REPLAY_STATE: ReplayState = {
  active: false,
  playing: false,
  cursorTime: null,
  remaining: 0,
  nextTime: null,
  intervalMs: 1000,
};

const NOOP_REPLAY_CONTROL: ChartReplayControl = {
  async start() {},
  step: () => false,
  stepUpdate: () => false,
  play() {},
  pause() {},
  stop() {},
  setTicks() {},
  get state() {
    return IDLE_REPLAY_STATE;
  },
  get bounds() {
    return null;
  },
  on: () => () => undefined,
};

export interface KLineChartFallbackInstance {
  dispose?(): void;
  resize?(): void;
}

export interface KLineChartRuntime {
  init(container: HTMLElement): KLineChartFallbackInstance;
}

/**
 * Optional fallback contract implementing `ChartSurfaceAdapter` for KLineChart v10.
 * Kept free of any `klinecharts` package dependency so the production workbench bundle
 * stays strictly on `@luxalgo/vela` (Apache-2.0) unless an explicit runtime is injected.
 */
export class KLineChartAdapter implements ChartSurfaceAdapter {
  readonly replay: ChartReplayControl = NOOP_REPLAY_CONTROL;
  private instance: KLineChartFallbackInstance | null = null;
  private readonly runtime: KLineChartRuntime | null;

  constructor(runtime: KLineChartRuntime | null = null) {
    this.runtime = runtime;
  }

  mount(container: HTMLElement, _options: ChartMountOptions): void {
    if (!this.runtime) {
      throw new Error(
        "KLineChartAdapter fallback requires an explicit KLineChartRuntime; VelaChartAdapter is the default.",
      );
    }
    this.instance = this.runtime.init(container);
  }

  async setMarket(): Promise<void> {}
  setBars(_bars: CompactBar[]): void {}
  updateLiveBar(_event: KlineEvent): void {}
  setIndicators(_series: IndicatorSeries[]): void {}
  setTradeMarkers(_trades: TradeExecution[]): void {}
  setSystemLevels(_levels: KeyLevel[]): void {}
  setAgentAnnotations(_annotations: AgentAnnotationRecord[]): void {}
  setExecutionBarriers(_barriers: ExecutionBarrierItem[]): void {}
  cancelBarrierDragConfirm(): void {}
  markBarrierDragPendingAck(_commandId: string, _proposal: BarrierDragProposal): void {}
  resolveBarrierDragPendingAck(_commandId?: string): void {}
  setStagedDrafts(_drafts: DraftRecord[]): void {}
  loadHumanDrawings(_drawings: DrawingRecord[]): void {}
  setReplayDrawingFilter(_filter: ReplayDrawingFilter): void {}
  setDrawingTool(_tool: string | null): void {}
  armOrderGesture(_direction: "LONG" | "SHORT" | null): void {}
  clearOrderGesture(): void {}
  selectHumanDrawing(_drawingId: string | null): void {}
  exportDrawingsDocument(): DrawingsDocument | null {
    return null;
  }
  setTheme(_theme: Theme): void {}
  setLocale(_locale: Locale): void {}
  setReadOnly(_readOnly: boolean): void {}
  setBackendMode(_backend: RenderBackendMode): void {}
  setExternalCrosshair(_time: number | null, _price?: number | null): void {}
  setVisibleRange(_range: { from: number; to: number }): void {}
  getVisibleRange(): { from: number; to: number } | null {
    return null;
  }
  getOldestLoadedBarTime(): number | null {
    return null;
  }
  async refresh(): Promise<void> {}
  resize(): void {
    this.instance?.resize?.();
  }
  destroy(): void {
    this.instance?.dispose?.();
    this.instance = null;
  }
}
