import type {
  DrawingsDocument,
  ReplayBounds,
  ReplayStartOptions,
  ReplayState,
  ReplayTickSource,
  TradeExecution,
  VelaEventMap,
} from "@luxalgo/vela";
import type {
  AgentAnnotationRecord,
  CompactBar,
  DrawingRecord,
  IndicatorSeries,
  KeyLevel,
  KlineEvent,
  PaperRound,
  SnapshotMeta,
} from "@talaria/contracts";

import type { Locale } from "../lib/i18n";
import type { Theme } from "../lib/theme";

export const VELA_ATTRIBUTION = {
  label: "Vela",
  url: "https://velacharts.dev",
} as const;

export type RenderBackendMode = "auto" | "webgl2" | "canvas2d";
export type DrawingSyncSource = "load" | "mutation";

export interface ChartStats {
  loaded: number;
  gaps: number;
}

export interface ExecutionBarrierItem {
  round_id: string;
  sub_account_id: string;
  direction: "LONG" | "SHORT";
  stop: string;
  take_profit: string[];
}

export function roundsToBarriers(rounds: readonly PaperRound[]): ExecutionBarrierItem[] {
  if (!Array.isArray(rounds)) return [];
  return rounds.map((round) => ({
    round_id: round.round_id,
    sub_account_id: round.sub_account_id,
    direction: round.direction,
    stop: round.stop,
    take_profit: Array.isArray(round.take_profit) ? round.take_profit : [],
  }));
}

export interface ChartMountOptions {
  chartId: string;
  instrumentId: string;
  timeframe: string;
  pricePrecision: number;
  volumePrecision: number;
  theme: Theme;
  locale: Locale;
  readOnly?: boolean;
  live?: boolean;
  snapshotCutoff?: number | null;
  onStats?: (stats: ChartStats) => void;
  onSnapshot?: (snapshot: SnapshotMeta | null) => void;
  onCrosshairMove?: (event: { time: number | null; price: number | null }) => void;
  onViewportLeftEdge?: (oldestLoadedTs: number) => void;
  onBackendChange?: (backend: RenderBackendMode) => void;
  onSelectKeyLevel?: (level: KeyLevel) => void;
  onSelectAnnotation?: (annotation: AgentAnnotationRecord) => void;
  onCreateAlertFromDrawing?: (payload: {
    drawingId: string;
    price: number | null;
    type: string;
  }) => void;
  onDrawingsChanged?: (drawings: DrawingRecord[], source: DrawingSyncSource) => void;
  onDrawingSelect?: (drawingId: string | null) => void;
  onDrawingError?: (message: string) => void;
  onLoadError?: (error: unknown) => void;
}

export type ReplayEventName =
  | "replay:start"
  | "replay:step"
  | "replay:tick"
  | "replay:play"
  | "replay:pause"
  | "replay:end";

export interface ChartReplayControl {
  start(opts: ReplayStartOptions): Promise<void>;
  step(): boolean;
  stepUpdate(): boolean;
  play(intervalMs?: number): void;
  pause(): void;
  stop(): void;
  setTicks(source: ReplayTickSource | null): void;
  readonly state: ReplayState;
  readonly bounds: ReplayBounds | null;
  on<K extends ReplayEventName>(
    event: K,
    handler: (payload: VelaEventMap[K]) => void,
  ): () => void;
}

export interface ReplayDrawingFilter {
  active: boolean;
  cursorTime: number | null;
  showAll: boolean;
}

/**
 * Narrow surface contract isolating the charting library (`VelaChartAdapter` primary,
 * `KLineChartAdapter` optional fallback) from React dockview workspace components.
 */
export interface ChartSurfaceAdapter {
  readonly replay: ChartReplayControl;
  mount(container: HTMLElement, options: ChartMountOptions): void;
  setMarket(params: {
    instrumentId: string;
    timeframe: string;
    pricePrecision: number;
    volumePrecision: number;
    live?: boolean;
    snapshotCutoff?: number | null;
  }): Promise<void>;
  setBars(bars: CompactBar[], opts?: { preserveView?: boolean }): void;
  updateLiveBar(event: KlineEvent): void;
  setIndicators(series: IndicatorSeries[]): void;
  setTradeMarkers(trades: TradeExecution[]): void;
  setSystemLevels(
    levels: KeyLevel[],
    opts?: { precision?: number; highlightId?: string | null },
  ): void;
  setAgentAnnotations(
    annotations: AgentAnnotationRecord[],
    opts?: { timeframe?: string; highlightId?: string | null },
  ): void;
  setExecutionBarriers(barriers: ExecutionBarrierItem[]): void;
  loadHumanDrawings(drawings: DrawingRecord[]): void;
  setReplayDrawingFilter(filter: ReplayDrawingFilter): void;
  setDrawingTool(tool: string | null): void;
  selectHumanDrawing(drawingId: string | null): void;
  exportDrawingsDocument(): DrawingsDocument | null;
  setTheme(theme: Theme): void;
  setLocale(locale: Locale): void;
  setReadOnly(readOnly: boolean): void;
  setBackendMode(backend: RenderBackendMode): void;
  setExternalCrosshair(time: number | null, price?: number | null): void;
  setVisibleRange(range: { from: number; to: number }): void;
  getVisibleRange(): { from: number; to: number } | null;
  getOldestLoadedBarTime(): number | null;
  refresh(): Promise<void>;
  resize(): void;
  destroy(): void;
}
