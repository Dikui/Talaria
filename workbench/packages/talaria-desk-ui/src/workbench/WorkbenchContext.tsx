import { createContext, useContext } from "react";
import type {
  AgentAnnotationRecord,
  DrawingRecord,
  IndicatorName,
  InstrumentSpec,
  KeyLevel,
} from "@talaria/contracts";

import type { ChartStats, RenderBackendMode } from "../chart/adapter";
import type { AlertFormSeed } from "../features/alerts/AlertPanel";
import type { AlertsController } from "../features/alerts/useAlerts";
import type { AnnotationsController } from "../features/annotations/useAnnotations";
import type { IndicatorDefaults, IndicatorSetting, IndicatorSettings } from "../features/indicators/config";
import type { KeyLevelFilter } from "../features/key-levels/model";
import type { KeyLevelsController } from "../features/key-levels/useKeyLevels";
import type { ScannerController } from "../features/scanner/useScanner";
import type { Locale } from "../lib/i18n";
import type { Theme } from "../lib/theme";
import type { LocateTarget, PanelLink } from "./link";

export interface ActiveChartTelemetry {
  paneId: string;
  instrumentId: string;
  timeframe: string;
  stats: ChartStats;
  backend: RenderBackendMode;
  snapshotId: string | null;
  cutoffTs: number | null;
}

export interface WorkbenchContextValue {
  theme: Theme;
  locale: Locale;
  readOnly: boolean;
  instruments: InstrumentSpec[];
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

  drawings: DrawingRecord[];
  selectedDrawingId: string | null;
  onDrawingsUpdated: (drawings: DrawingRecord[]) => void;
  onDrawingSelected: (drawingId: string | null) => void;
  onDeleteDrawing: (drawingId: string) => Promise<void>;
  onClearDrawings: () => Promise<void>;

  alertsController: AlertsController;
  alertSeed: AlertFormSeed | null;
  onSeedAlert: (seed: Omit<AlertFormSeed, "nonce">) => void;

  scannerController: ScannerController;
  annotationsController: AnnotationsController;

  onSelectInstrument: (instrumentId: string) => void;
  onLocateTarget: (target: LocateTarget) => void;
  onPopoutChart: (params: {
    paneId: string;
    instrumentId: string;
    timeframe: string;
  }) => void;
  onOpenRightTab: (tab: "watchlist" | "alerts" | "levels" | "scanner" | "drawings" | "annotations") => void;
  onAdoptKeyLevel: (level: KeyLevel) => Promise<void>;
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
