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
  DraftRecord,
  DrawingRecord,
  IndicatorSeries,
  KeyLevel,
  KlineEvent,
  PaperRound,
  SnapshotMeta,
  StructureFeature,
} from "@talaria/contracts";

import type { Locale } from "../lib/i18n";
import type { Theme } from "../lib/theme";
import type {
  BarrierDragDisabledEvent,
  BarrierDragDisabledReason,
  BarrierDragOverlay,
  BarrierDragProposal,
} from "./barrierDragController";
import type { OrderGestureGeometry } from "./orderGestureTracker";

export type {
  BarrierDragDisabledEvent,
  BarrierDragDisabledReason,
  BarrierDragOverlay,
  BarrierDragProposal,
} from "./barrierDragController";
export type { OrderGestureGeometry } from "./orderGestureTracker";

export interface OrderGestureCreateDraftPayload {
  drawingId: string;
  direction: "LONG" | "SHORT";
  entry: string;
  stop: string;
  target: string;
  validGeometry: boolean;
}

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

export function roundsToBarriers(rounds: readonly PaperRound[]): ExecutionBarrierItem[] {
  if (!Array.isArray(rounds)) return [];
  return rounds.map((round) => ({
    round_id: round.round_id,
    sub_account_id: round.sub_account_id,
    direction: round.direction,
    stop: round.stop,
    take_profit: Array.isArray(round.take_profit) ? round.take_profit : [],
    phase: round.phase ?? null,
    entry_low: round.entry_low ?? null,
    entry_high: round.entry_high ?? null,
    entry_price: round.entry_price ?? null,
    mark_price: round.mark_price ?? null,
    unrealized_pnl: round.unrealized_pnl ?? null,
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
  onSelectStructure?: (structure: StructureFeature) => void;
  onSelectAnnotation?: (annotation: AgentAnnotationRecord) => void;
  onSelectStagedDraft?: (draft: DraftRecord) => void;
  onCreateAlertFromDrawing?: (payload: {
    drawingId: string;
    price: number | null;
    type: string;
  }) => void;
  onOrderGestureChange?: (gesture: OrderGestureGeometry | null) => void;
  onCreateDraftFromOrderGesture?: (payload: OrderGestureCreateDraftPayload) => void;
  onDrawingToolChange?: (tool: string | null) => void;
  onBarrierDragEnd?: (proposal: BarrierDragProposal) => void;
  onBarrierDragDisabled?: (event: BarrierDragDisabledEvent) => void;
  onBarrierPendingAckTimeout?: (overlay: BarrierDragOverlay) => void;
  onBarrierOverlayChange?: (overlay: BarrierDragOverlay | null) => void;
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
  setSystemStructures(
    structures: StructureFeature[],
    opts?: { precision?: number; highlightId?: string | null },
  ): void;
  setAgentAnnotations(
    annotations: AgentAnnotationRecord[],
    opts?: { timeframe?: string; highlightId?: string | null },
  ): void;
  setExecutionBarriers(barriers: ExecutionBarrierItem[]): void;
  cancelBarrierDragConfirm(): void;
  markBarrierDragPendingAck(commandId: string, proposal: BarrierDragProposal): void;
  resolveBarrierDragPendingAck(commandId?: string): void;
  setStagedDrafts(
    drafts: DraftRecord[],
    opts?: { precision?: number; highlightId?: string | null },
  ): void;
  loadHumanDrawings(drawings: DrawingRecord[]): void;
  setReplayDrawingFilter(filter: ReplayDrawingFilter): void;
  setDrawingTool(tool: string | null): void;
  armOrderGesture(direction: "LONG" | "SHORT" | null): void;
  clearOrderGesture(): void;
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
