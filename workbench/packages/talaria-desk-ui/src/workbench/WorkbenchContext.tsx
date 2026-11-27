import { createContext, useContext } from "react";
import type {
  AgentAnnotationRecord,
  DrawingRecord,
  IndicatorName,
  InstrumentSpec,
  KeyLevel,
  RunRoundRecord,
  StructureFeature,
  StructureSet,
} from "@talaria/contracts";

import type { ChartStats, RenderBackendMode } from "../chart/adapter";
import type { AlertFormSeed } from "../features/alerts/AlertPanel";
import type { AlertsController } from "../features/alerts/useAlerts";
import type { AnnotationsController } from "../features/annotations/useAnnotations";
import type { IndicatorDefaults, IndicatorSetting, IndicatorSettings } from "../features/indicators/config";
import type { KeyLevelFilter } from "../features/key-levels/model";
import type { KeyLevelsController } from "../features/key-levels/useKeyLevels";
import type { StructureFilter } from "../features/structures/model";
import type { StructuresController } from "../features/structures/useStructures";
import type { DraftSeed } from "../features/paper/DraftForm";
import type { RunsController } from "../features/runs/useRuns";
import type { ScannerController } from "../features/scanner/useScanner";
import type { Locale } from "../lib/i18n";
import type { Theme } from "../lib/theme";
import type { LocateTarget, PanelLink } from "./link";

export interface ReplayTriggerRequest {
  nonce: number;
  instrumentId: string;
  timeframe?: string;
  fromTs: number;
}

export interface ChartJumpRequest {
  nonce: number;
  instrumentId: string;
  timeframe?: string;
  openedTs: number;
  closedTs?: number | null;
}

export interface ActiveChartTelemetry {
  paneId: string;
  instrumentId: string;
  timeframe: string;
  stats: ChartStats;
  backend: RenderBackendMode;
  snapshotId: string | null;
  cutoffTs: number | null;
}

export interface SymbolSearchOptions {
  title?: string;
  actionLabel?: string;
  currentInstrumentId?: string;
  onPicked?: (spec: InstrumentSpec) => void | Promise<void>;
}

export interface WorkbenchContextValue {
  theme: Theme;
  locale: Locale;
  readOnly: boolean;
  instruments: InstrumentSpec[];
  onInstrumentsChanged: (next: InstrumentSpec[]) => void;
  onOpenSymbolSearch: (options?: SymbolSearchOptions) => void;
  feedMode: string | null;
  primaryLink: PanelLink;
  activeInstrumentId: string;
  activeTimeframe: string;
  activeSnapshotId: string | null;
  activeStats: ChartStats;
  activeStreamStatus: string;
  activePaneId: string;
  setActivePaneId: (paneId: string) => void;
  updatePaneTelemetry: (telemetry: ActiveChartTelemetry) => void;

  indicatorSettings: IndicatorSettings;
  indicatorDefaults: IndicatorDefaults;
  savingIndicatorDefault: IndicatorName | null;
  onUpdateIndicator: (name: IndicatorName, patch: Partial<IndicatorSetting>) => void;
  onSaveIndicatorDefault: (name: IndicatorName) => void;
  onResetIndicatorDefault: (name: IndicatorName) => void;

  keyLevelFilter: KeyLevelFilter;
  onKeyLevelFilterChange: (next: KeyLevelFilter) => void;
  highlightedLevelId: string | null;
  onHoverLevelId: (levelId: string | null) => void;
  keyLevelsController: KeyLevelsController;

  structureFilter: StructureFilter;
  onStructureFilterChange: (next: StructureFilter) => void;
  highlightedStructureId: string | null;
  onHoverStructureId: (structureId: string | null) => void;
  structuresController: StructuresController;
  onStreamStructures: (structures: StructureSet) => void;

  drawings: DrawingRecord[];
  selectedDrawingId: string | null;
  onDrawingsUpdated: (drawings: DrawingRecord[]) => void;
  onDrawingSelected: (drawingId: string | null) => void;
  onDeleteDrawing: (drawingId: string) => Promise<void>;
  onClearDrawings: () => Promise<void>;

  alertsController: AlertsController;
  alertSeed: AlertFormSeed | null;
  onSeedAlert: (seed: Omit<AlertFormSeed, "nonce">) => void;

  draftSeed: (DraftSeed & { nonce: number }) | null;
  onSeedDraft: (seed: DraftSeed) => void;
  clearDraftSeed: (nonce?: number) => void;
  selectedDraftId: string | null;
  onSelectDraft: (draftId: string | null) => void;
  draftsRevision: number;
  notifyDraftsChanged: () => void;

  scannerController: ScannerController;
  annotationsController: AnnotationsController;
  runsController: RunsController;
  selectedRunRound: RunRoundRecord | null;

  replayActive: boolean;
  onPaneReplayActiveChange: (paneId: string, active: boolean) => void;
  replayRequest: ReplayTriggerRequest | null;
  clearReplayRequest: (nonce: number) => void;
  jumpRequest: ChartJumpRequest | null;
  clearJumpRequest: (nonce: number) => void;

  onSelectInstrument: (instrumentId: string) => void;
  onLocateTarget: (target: LocateTarget) => void;
  onJumpToRound: (round: RunRoundRecord, timeframe?: string) => void;
  onReplayBeforeRound: (params: {
    instrumentId: string;
    timeframe?: string;
    fromTs: number;
  }) => void;
  onPopoutChart: (params: {
    paneId: string;
    instrumentId: string;
    timeframe: string;
  }) => void;
  onOpenRightTab: (
    tab:
      | "watchlist"
      | "alerts"
      | "levels"
      | "scanner"
      | "drawings"
      | "annotations"
      | "runs"
      | "drafts"
      | "account",
  ) => void;
  onAdoptKeyLevel: (level: KeyLevel) => Promise<void>;
  onAdoptStructure: (structure: StructureFeature) => Promise<void>;
  onAdoptAnnotation: (annotation: AgentAnnotationRecord) => Promise<void>;
}

const WorkbenchContext = createContext<WorkbenchContextValue | null>(null);

export const WorkbenchProvider = WorkbenchContext.Provider;

export function useWorkbenchContext(): WorkbenchContextValue {
  const ctx = useContext(WorkbenchContext);
  if (!ctx) {
    throw new Error("useWorkbenchContext must be used inside WorkbenchProvider");
  }
  return ctx;
}
