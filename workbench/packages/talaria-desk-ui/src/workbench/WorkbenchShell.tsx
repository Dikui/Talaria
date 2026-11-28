import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  DockviewReact,
  themeDark,
  themeLight,
  type DockviewApi,
  type DockviewReadyEvent,
  type IDockviewPanelProps,
} from "dockview-react";
import {
  Activity,
  Bookmark,
  Briefcase,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  History,
  Layers,
  LayoutGrid,
  Moon,
  PanelRight,
  Plus,
  RotateCcw,
  Sun,
} from "lucide-react";
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

import { VELA_ATTRIBUTION } from "../chart/adapter";
import { useWebglBudget } from "../chart/webglBudget";
import { AlertToast, type AlertFormSeed } from "../features/alerts/AlertPanel";
import { useAlerts } from "../features/alerts/useAlerts";
import { useAnnotations } from "../features/annotations/useAnnotations";
import { deskSocketHub } from "../features/feed/deskSocketHub";
import {
  isMobileDrawerViewport,
  isReadOnlyViewport,
} from "../features/drawings/responsive";
import {
  INDICATOR_DEFAULTS,
  defaultsFromParams,
  operatorParams,
  updateIndicatorSetting,
  type IndicatorDefaults,
  type IndicatorSetting,
  type IndicatorSettings,
} from "../features/indicators/config";
import { InstrumentSearchModal } from "../features/instruments/InstrumentSearchModal";
import {
  visibleKeyLevels,
  type KeyLevelFilter,
} from "../features/key-levels/model";
import { useKeyLevels } from "../features/key-levels/useKeyLevels";
import {
  structureMidPrice,
  visibleStructures,
  type StructureFilter,
} from "../features/structures/model";
import { useStructures } from "../features/structures/useStructures";
import type { DraftSeed } from "../features/paper/DraftForm";
import { RunsPanel } from "../features/runs/RunsPanel";
import { useRuns } from "../features/runs/useRuns";
import { useScanner } from "../features/scanner/useScanner";
import { WatchlistPanel } from "../features/watchlist/WatchlistPanel";
import { api, getApiDataDir } from "../lib/api";
import { getLocale, setLocale, t, useLocale } from "../lib/i18n";
import {
  getTauriShellSettings,
  isTauriRuntime,
  openTauriPopoutWindow,
  setTauriKeepRunningInBackground,
} from "../lib/tauriBridge";
import { applyTheme, THEME_STORAGE_KEY, type Theme } from "../lib/theme";
import {
  loadIndicatorSettings,
  loadKeyLevelFilter,
  loadStructureFilter,
  mergeIndicatorDefaults,
  saveIndicatorSettings,
  saveKeyLevelFilter,
  saveStructureFilter,
} from "./chartPreferences";
import { ChartPane } from "./ChartPane";
import { LayersPanel } from "./LayersPanel";
import {
  MOBILE_TABS,
  applyPresetLayout,
  clampBottomDrawerHeight,
  clampRightDrawerWidth,
  clearSavedLayout,
  loadLayoutFromStorage,
  loadShellUiState,
  resolveMobileNavigation,
  resolvePanelNavigation,
  saveLayoutToStorage,
  saveShellUiState,
  toggleRightDrawerState,
  type ChartPaneParams,
  type LayoutPreset,
  type LegacyOrConsolidatedPanelTarget,
  type MobileTabId,
  type RightDrawerId,
  type ShellUiState,
} from "./layoutStore";
import type { LocateTarget } from "./link";
import { MonitorPanel } from "./MonitorPanel";
import { DESK_PATH, consolidatedLabel, type PanelName } from "./panels";
import { TradePanel } from "./TradePanel";
import { useLinkSnapshot, usePanelLink } from "./usePanelLink";
import {
  WorkbenchProvider,
  useWorkbenchContext,
  type ActiveChartTelemetry,
  type ChartJumpRequest,
  type ReplayTriggerRequest,
  type SymbolSearchOptions,
  type WorkbenchContextValue,
} from "./WorkbenchContext";

function DockChartPanel({ api: panelApi, params }: IDockviewPanelProps<ChartPaneParams>) {
  const paneId = params.paneId || panelApi.id;
  const onTitleChange = useCallback(
    (title: string) => {
      panelApi.setTitle(title);
    },
    [panelApi],
  );
  return (
    <ChartPane
      paneId={paneId}
      initialTimeframe={params.initialTimeframe || "1m"}
      syncTimeframe={params.syncTimeframe ?? false}
      onTitleChange={onTitleChange}
    />
  );
}

const DOCKVIEW_COMPONENTS = {
  chart: DockChartPanel,
};

export interface WorkbenchShellProps {
  initialTheme: Theme;
  onThemeChange: (next: Theme) => void;
  popoutConfig?: {
    panel: PanelName;
    timeframe: string;
    instrumentId: string;
    paneId: string;
  } | null;
}

export function WorkbenchShell({
  initialTheme,
  onThemeChange,
  popoutConfig = null,
}: WorkbenchShellProps) {
  const locale = useLocale();
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [viewportWidth, setViewportWidth] = useState(() =>
    typeof window !== "undefined" ? window.innerWidth : 1440,
  );
  const readOnly = isReadOnlyViewport(viewportWidth);
  const isMobile = isMobileDrawerViewport(viewportWidth);

  const primaryLink = usePanelLink({
    key: popoutConfig?.paneId ?? "workbench-primary",
    panel: popoutConfig?.panel ?? "chart",
    initialTimeframe: popoutConfig?.timeframe ?? "1m",
    syncTimeframe: Boolean(popoutConfig),
  });
  const primarySnap = useLinkSnapshot(primaryLink);

  useEffect(() => {
    if (popoutConfig?.instrumentId && primarySnap.instrumentId !== popoutConfig.instrumentId) {
      primaryLink.setSymbol(popoutConfig.instrumentId, popoutConfig.timeframe);
    }
    // Run only once on popout mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const [instruments, setInstruments] = useState<InstrumentSpec[]>([]);
  const [symbolSearch, setSymbolSearch] = useState<{
    open: boolean;
    options?: SymbolSearchOptions;
  }>({ open: false });
  const [feedMode, setFeedMode] = useState<string | null>(null);
  const [activePaneId, setActivePaneId] = useState<string>(
    popoutConfig?.paneId ?? "chart-1m",
  );
  const [telemetry, setTelemetry] = useState<ActiveChartTelemetry>({
    paneId: popoutConfig?.paneId ?? "chart-1m",
    instrumentId: primarySnap.instrumentId,
    timeframe: primarySnap.timeframe,
    stats: { loaded: 0, gaps: 0 },
    backend: "auto",
    snapshotId: null,
    cutoffTs: null,
  });

  const [indicatorDefaults, setIndicatorDefaults] =
    useState<IndicatorDefaults>(INDICATOR_DEFAULTS);
  const indicatorDefaultsRef = useRef<IndicatorDefaults>(INDICATOR_DEFAULTS);
  const [indicatorSettings, setIndicatorSettings] = useState<IndicatorSettings>(() =>
    loadIndicatorSettings(INDICATOR_DEFAULTS),
  );
  const [savingIndicatorDefault, setSavingIndicatorDefault] =
    useState<IndicatorName | null>(null);

  const [keyLevelFilter, setKeyLevelFilter] = useState<KeyLevelFilter>(() =>
    loadKeyLevelFilter(),
  );
  const [highlightedLevelId, setHighlightedLevelId] = useState<string | null>(null);
  const [structureFilter, setStructureFilter] = useState<StructureFilter>(() =>
    loadStructureFilter(),
  );
  const [highlightedStructureId, setHighlightedStructureId] = useState<string | null>(null);
  const [streamedStructures, setStreamedStructures] = useState<StructureSet | null>(null);
  const [drawings, setDrawings] = useState<DrawingRecord[]>([]);
  const [selectedDrawingId, setSelectedDrawingId] = useState<string | null>(null);
  const [alertSeed, setAlertSeed] = useState<AlertFormSeed | null>(null);
  const [deniedAlerts, setDeniedAlerts] = useState<string[]>([]);
  const [draftSeed, setDraftSeed] = useState<(DraftSeed & { nonce: number }) | null>(null);
  const [selectedDraftId, setSelectedDraftId] = useState<string | null>(null);
  const [draftsRevision, setDraftsRevision] = useState(0);

  const [shellUi, setShellUi] = useState<ShellUiState>(() => loadShellUiState());
  const [mobileDrawerOpen, setMobileDrawerOpen] = useState(false);
  const [mobileTab, setMobileTab] = useState<MobileTabId>("watchlist");
  const [tauriKeepRunning, setTauriKeepRunning] = useState<boolean | null>(null);
  const [activeDataDir, setActiveDataDir] = useState<string | null>(() => getApiDataDir());

  const dockApiRef = useRef<DockviewApi | null>(null);
  const saveTimerRef = useRef<number | null>(null);
  const extraPaneCounterRef = useRef(1);

  const budget = useWebglBudget();
  const alertsController = useAlerts();
  const scannerController = useScanner(true);
  const runsController = useRuns();
  const annotationsController = useAnnotations({
    instrumentId: telemetry.instrumentId || primarySnap.instrumentId,
    onDrawingsChanged: primaryLink.notifyDrawingsChanged,
  });
  const keyLevelsController = useKeyLevels({
    snapshotId: telemetry.snapshotId,
    instrumentId: telemetry.instrumentId || primarySnap.instrumentId,
    timeframe: telemetry.timeframe || primarySnap.timeframe,
    streamed: null,
  });
  const structuresController = useStructures({
    snapshotId: telemetry.snapshotId,
    instrumentId: telemetry.instrumentId || primarySnap.instrumentId,
    timeframe: telemetry.timeframe || primarySnap.timeframe,
    scale: structureFilter.scale,
    htfTimeframes: structureFilter.htfTimeframes,
    streamed: streamedStructures,
  });

  const [replayPanes, setReplayPanes] = useState<Record<string, boolean>>({});
  const replayActive = useMemo(
    () => Object.values(replayPanes).some(Boolean),
    [replayPanes],
  );
  const [replayRequest, setReplayRequest] = useState<ReplayTriggerRequest | null>(null);
  const [jumpRequest, setJumpRequest] = useState<ChartJumpRequest | null>(null);

  const onPaneReplayActiveChange = useCallback((paneId: string, active: boolean) => {
    setReplayPanes((prev) => {
      if (Boolean(prev[paneId]) === active) return prev;
      return { ...prev, [paneId]: active };
    });
  }, []);

  const clearReplayRequest = useCallback((nonce: number) => {
    setReplayRequest((prev) => (prev && prev.nonce === nonce ? null : prev));
  }, []);

  const clearJumpRequest = useCallback((nonce: number) => {
    setJumpRequest((prev) => (prev && prev.nonce === nonce ? null : prev));
  }, []);

  const selectedRunRound = useMemo(
    () =>
      runsController.rounds.find((r) => r.round_id === runsController.selectedRoundId) ??
      null,
    [runsController.rounds, runsController.selectedRoundId],
  );

  useEffect(() => {
    const onResize = () => setViewportWidth(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  useEffect(() => {
    return deskSocketHub.subscribeGlobalEvents({
      onAlert: (trigger) => alertsController.ingestTrigger(trigger),
      onAlertQuota: (denied) => setDeniedAlerts(denied),
      onScanner: (trigger) => scannerController.ingest(trigger),
    });
  }, [alertsController.ingestTrigger, scannerController.ingest]);

  useEffect(() => {
    saveShellUiState(shellUi);
  }, [shellUi]);

  const onOpenSymbolSearch = useCallback((options?: SymbolSearchOptions) => {
    setSymbolSearch({ open: true, options });
  }, []);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setSymbolSearch((prev) => (prev.open ? { open: false } : { open: true }));
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    let active = true;
    api
      .health()
      .then((h) => active && setFeedMode(h.feed_mode))
      .catch(() => active && setFeedMode(null));
    api
      .instruments()
      .then((list) => active && setInstruments(list))
      .catch(() => active && setInstruments([]));
    api
      .indicatorDefaults()
      .then((res) => {
        if (!active) return;
        const defs = defaultsFromParams(res.defaults);
        const prevDefs = indicatorDefaultsRef.current;
        indicatorDefaultsRef.current = defs;
        setIndicatorDefaults(defs);
        setIndicatorSettings((prev) => mergeIndicatorDefaults(prev, prevDefs, defs));
      })
      .catch(() => undefined);

    if (isTauriRuntime()) {
      void getTauriShellSettings().then((settings) => {
        if (active && settings) {
          setTauriKeepRunning(settings.keep_running_in_background);
          if (settings.data_dir) {
            setActiveDataDir(settings.data_dir);
          }
        }
      });
    } else if (!getApiDataDir()) {
      void api
        .deskBootstrap()
        .then((info) => {
          if (active && info.data_dir) {
            setActiveDataDir(info.data_dir);
          }
        })
        .catch(() => undefined);
    }
    return () => {
      active = false;
    };
  }, []);

  const toggleTheme = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    applyTheme(next);
    onThemeChange(next);
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // Ignore.
    }
  };

  const toggleLocale = () => {
    const next = locale === "zh" ? "en" : "zh";
    setLocale(next);
    document.documentElement.lang = next === "zh" ? "zh-CN" : "en";
  };

  const onUpdateIndicator = useCallback(
    (name: IndicatorName, patch: Partial<IndicatorSetting>) => {
      setIndicatorSettings((prev) => {
        const next = updateIndicatorSetting(prev, indicatorDefaults, name, patch);
        saveIndicatorSettings(next);
        return next;
      });
    },
    [indicatorDefaults],
  );

  const onKeyLevelFilterChange = useCallback((next: KeyLevelFilter) => {
    setKeyLevelFilter(next);
    saveKeyLevelFilter(next);
  }, []);

  const onStructureFilterChange = useCallback((next: StructureFilter) => {
    setStructureFilter(next);
    saveStructureFilter(next);
  }, []);

  const onSaveIndicatorDefault = useCallback(
    (name: IndicatorName) => {
      setSavingIndicatorDefault(name);
      void api
        .setIndicatorDefault(name, operatorParams(name, indicatorSettings[name]))
        .then((res) => setIndicatorDefaults(defaultsFromParams(res.defaults)))
        .catch(() => undefined)
        .finally(() => setSavingIndicatorDefault(null));
    },
    [indicatorSettings],
  );

  const onResetIndicatorDefault = useCallback((name: IndicatorName) => {
    setSavingIndicatorDefault(name);
    void api
      .resetIndicatorDefault(name)
      .then((res) => setIndicatorDefaults(defaultsFromParams(res.defaults)))
      .catch(() => undefined)
      .finally(() => setSavingIndicatorDefault(null));
  }, []);

  const onSelectInstrument = useCallback(
    (instrumentId: string) => {
      primaryLink.setSymbol(instrumentId);
    },
    [primaryLink],
  );

  const onLocateTarget = useCallback(
    (target: LocateTarget) => {
      primaryLink.locate(target);
      alertsController.markSeen();
    },
    [primaryLink, alertsController],
  );

  const onJumpToRound = useCallback(
    (round: RunRoundRecord, timeframe?: string) => {
      if (round.instrument_id !== (telemetry.instrumentId || primarySnap.instrumentId)) {
        primaryLink.setSymbol(round.instrument_id, timeframe);
      }
      if (round.opened_ts !== null && round.opened_ts !== undefined) {
        setJumpRequest({
          nonce: Date.now() + Math.random(),
          instrumentId: round.instrument_id,
          timeframe,
          openedTs: round.opened_ts,
          closedTs: round.closed_ts,
        });
      }
    },
    [primaryLink, telemetry.instrumentId, primarySnap.instrumentId],
  );

  const onReplayBeforeRound = useCallback(
    (params: { instrumentId: string; timeframe?: string; fromTs: number }) => {
      if (params.instrumentId !== (telemetry.instrumentId || primarySnap.instrumentId)) {
        primaryLink.setSymbol(params.instrumentId, params.timeframe);
      }
      setReplayRequest({
        nonce: Date.now() + Math.random(),
        instrumentId: params.instrumentId,
        timeframe: params.timeframe,
        fromTs: params.fromTs,
      });
    },
    [primaryLink, telemetry.instrumentId, primarySnap.instrumentId],
  );

  const onSeedAlert = useCallback((seed: Omit<AlertFormSeed, "nonce">) => {
    setAlertSeed({ ...seed, nonce: Date.now() + Math.random() });
  }, []);

  const clearAlertSeed = useCallback((nonce?: number) => {
    setAlertSeed((prev) => {
      if (!prev) return null;
      if (nonce !== undefined && prev.nonce !== nonce) return prev;
      return null;
    });
  }, []);

  const onSeedDraft = useCallback((seed: DraftSeed) => {
    setDraftSeed({ ...seed, nonce: Date.now() + Math.random() });
  }, []);

  const clearDraftSeed = useCallback((nonce?: number) => {
    setDraftSeed((prev) => {
      if (!prev) return null;
      if (nonce !== undefined && prev.nonce !== nonce) return prev;
      return null;
    });
  }, []);

  const notifyDraftsChanged = useCallback(() => {
    setDraftsRevision((n) => n + 1);
  }, []);

  const onOpenRightTab = useCallback(
    (tab: LegacyOrConsolidatedPanelTarget) => {
      if (isMobile) {
        const nav = resolveMobileNavigation(tab);
        setMobileTab(nav.tab);
        if (nav.monitorTab || nav.layerSource || nav.tradeTab) {
          setShellUi((prev) => ({
            ...prev,
            ...(nav.monitorTab ? { monitorTab: nav.monitorTab } : {}),
            ...(nav.layerSource ? { layerSource: nav.layerSource } : {}),
            ...(nav.tradeTab ? { tradeTab: nav.tradeTab } : {}),
          }));
        }
        setMobileDrawerOpen(true);
        return;
      }
      setShellUi((prev) => resolvePanelNavigation(prev, tab));
    },
    [isMobile],
  );

  const onPopoutChart = useCallback(
    (params: { paneId: string; instrumentId: string; timeframe: string }) => {
      const search = new URLSearchParams({
        popout: "1",
        panel: "chart",
        pane: params.paneId,
        instrument: params.instrumentId,
        timeframe: params.timeframe,
        lang: locale,
      });
      const urlPath = `${DESK_PATH}?${search.toString()}`;
      if (isTauriRuntime()) {
        void openTauriPopoutWindow({
          urlPath,
          title: `Talaria · ${params.instrumentId} (${params.timeframe})`,
          width: 960,
          height: 680,
        });
        return;
      }
      window.open(
        urlPath,
        `talaria-popout-${params.paneId}-${Date.now()}`,
        "popup=yes,width=960,height=680,resizable=yes",
      );
    },
    [locale],
  );

  const onAdoptKeyLevel = useCallback(
    async (level: KeyLevel) => {
      const drawing = await keyLevelsController.adopt(level);
      if (drawing) {
        primaryLink.notifyDrawingsChanged(level.instrument_id);
      }
    },
    [keyLevelsController, primaryLink],
  );

  const onAdoptStructure = useCallback(
    async (feature: StructureFeature) => {
      const drawing = await structuresController.adopt(feature);
      if (drawing) {
        primaryLink.notifyDrawingsChanged(feature.instrument_id);
      }
    },
    [structuresController, primaryLink],
  );

  const onAdoptAnnotation = useCallback(
    async (annotation: AgentAnnotationRecord) => {
      await annotationsController.adopt(annotation.annotation_id);
      primaryLink.notifyDrawingsChanged(annotation.instrument_id);
    },
    [annotationsController, primaryLink],
  );

  const onDeleteDrawing = useCallback(
    async (drawingId: string) => {
      await api.deleteDrawing(drawingId);
      setDrawings((prev) => prev.filter((d) => d.drawing_id !== drawingId));
      primaryLink.notifyDrawingsChanged(telemetry.instrumentId);
    },
    [primaryLink, telemetry.instrumentId],
  );

  const onClearDrawings = useCallback(async () => {
    await api.clearDrawings(telemetry.instrumentId);
    setDrawings([]);
    primaryLink.notifyDrawingsChanged(telemetry.instrumentId);
  }, [primaryLink, telemetry.instrumentId]);

  const contextValue: WorkbenchContextValue = useMemo(
    () => ({
      theme,
      locale,
      readOnly,
      instruments,
      onInstrumentsChanged: setInstruments,
      onOpenSymbolSearch,
      feedMode,
      primaryLink,
      activeInstrumentId: telemetry.instrumentId || primarySnap.instrumentId,
      activeTimeframe: telemetry.timeframe || primarySnap.timeframe,
      activeSnapshotId: telemetry.snapshotId,
      activeStats: telemetry.stats,
      activeStreamStatus: feedMode ?? "unknown",
      activePaneId,
      setActivePaneId,
      updatePaneTelemetry: setTelemetry,
      indicatorSettings,
      indicatorDefaults,
      savingIndicatorDefault,
      onUpdateIndicator,
      onSaveIndicatorDefault,
      onResetIndicatorDefault,
      keyLevelFilter,
      onKeyLevelFilterChange,
      highlightedLevelId,
      onHoverLevelId: setHighlightedLevelId,
      keyLevelsController,
      structureFilter,
      onStructureFilterChange,
      highlightedStructureId,
      onHoverStructureId: setHighlightedStructureId,
      structuresController,
      onStreamStructures: setStreamedStructures,
      drawings,
      selectedDrawingId,
      onDrawingsUpdated: setDrawings,
      onDrawingSelected: setSelectedDrawingId,
      onDeleteDrawing,
      onClearDrawings,
      alertsController,
      deniedAlerts,
      alertSeed,
      onSeedAlert,
      clearAlertSeed,
      draftSeed,
      onSeedDraft,
      clearDraftSeed,
      selectedDraftId,
      onSelectDraft: setSelectedDraftId,
      draftsRevision,
      notifyDraftsChanged,
      scannerController,
      annotationsController,
      runsController,
      selectedRunRound,
      replayActive,
      onPaneReplayActiveChange,
      replayRequest,
      clearReplayRequest,
      jumpRequest,
      clearJumpRequest,
      onSelectInstrument,
      onLocateTarget,
      onJumpToRound,
      onReplayBeforeRound,
      onPopoutChart,
      onOpenRightTab,
      onAdoptKeyLevel,
      onAdoptStructure,
      onAdoptAnnotation,
    }),
    [
      theme,
      locale,
      readOnly,
      instruments,
      onOpenSymbolSearch,
      feedMode,
      primaryLink,
      telemetry,
      primarySnap.instrumentId,
      primarySnap.timeframe,
      activePaneId,
      indicatorSettings,
      indicatorDefaults,
      savingIndicatorDefault,
      onUpdateIndicator,
      onSaveIndicatorDefault,
      onResetIndicatorDefault,
      keyLevelFilter,
      onKeyLevelFilterChange,
      highlightedLevelId,
      keyLevelsController,
      structureFilter,
      onStructureFilterChange,
      highlightedStructureId,
      structuresController,
      drawings,
      selectedDrawingId,
      onDeleteDrawing,
      onClearDrawings,
      alertsController,
      deniedAlerts,
      alertSeed,
      onSeedAlert,
      clearAlertSeed,
      draftSeed,
      onSeedDraft,
      clearDraftSeed,
      selectedDraftId,
      draftsRevision,
      notifyDraftsChanged,
      scannerController,
      annotationsController,
      runsController,
      selectedRunRound,
      replayActive,
      onPaneReplayActiveChange,
      replayRequest,
      clearReplayRequest,
      jumpRequest,
      clearJumpRequest,
      onSelectInstrument,
      onLocateTarget,
      onJumpToRound,
      onReplayBeforeRound,
      onPopoutChart,
      onOpenRightTab,
      onAdoptKeyLevel,
      onAdoptStructure,
      onAdoptAnnotation,
    ],
  );

  const handleDockReady = useCallback((event: DockviewReadyEvent) => {
    dockApiRef.current = event.api;
    const saved = loadLayoutFromStorage();
    if (saved) {
      try {
        event.api.fromJSON(saved);
      } catch {
        applyPresetLayout(event.api, "multi-tf");
      }
    } else {
      applyPresetLayout(event.api, "multi-tf");
    }

    event.api.onDidLayoutChange(() => {
      if (saveTimerRef.current !== null) {
        window.clearTimeout(saveTimerRef.current);
      }
      saveTimerRef.current = window.setTimeout(() => {
        saveTimerRef.current = null;
        if (dockApiRef.current) {
          saveLayoutToStorage(dockApiRef.current);
        }
      }, 250);
    });
  }, []);

  const addChartPane = () => {
    const dockApi = dockApiRef.current;
    if (!dockApi) return;
    const idx = extraPaneCounterRef.current++;
    const tf = idx % 2 === 0 ? "15m" : "4h";
    const id = `chart-extra-${idx}`;
    const refPanel = dockApi.getPanel("chart-1m") ?? dockApi.panels[0];
    dockApi.addPanel<ChartPaneParams>({
      id,
      component: "chart",
      title: `${primarySnap.instrumentId} · ${tf}`,
      params: { paneId: id, initialTimeframe: tf, syncTimeframe: false },
      ...(refPanel ? { position: { referencePanel: refPanel, direction: "below" } } : {}),
    });
  };

  const applyPreset = (preset: LayoutPreset) => {
    const dockApi = dockApiRef.current;
    if (!dockApi) return;
    applyPresetLayout(dockApi, preset);
    saveLayoutToStorage(dockApi);
  };

  const resetLayout = () => {
    clearSavedLayout();
    applyPreset("multi-tf");
    setShellUi((prev) => ({
      ...prev,
      rightDrawer: prev.mode === "live" ? "watchlist" : null,
      rightDrawerWidth: 340,
      tradeDrawerHeight: 280,
      backtestDrawerHeight: 320,
    }));
  };

  const toggleRightRail = (id: RightDrawerId) => {
    setShellUi((prev) => toggleRightDrawerState(prev, id));
  };

  const startResizeRightDrawer = (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = shellUi.rightDrawerWidth;
    const onMove = (moveEvent: PointerEvent) => {
      const delta = startX - moveEvent.clientX;
      setShellUi((prev) => ({
        ...prev,
        rightDrawerWidth: clampRightDrawerWidth(startWidth + delta),
      }));
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  const startResizeBottomDrawer = (
    event: React.PointerEvent<HTMLDivElement>,
    kind: "trade" | "backtest",
  ) => {
    event.preventDefault();
    const startY = event.clientY;
    const startHeight =
      kind === "trade" ? shellUi.tradeDrawerHeight : shellUi.backtestDrawerHeight;
    const onMove = (moveEvent: PointerEvent) => {
      const delta = startY - moveEvent.clientY;
      const nextHeight = clampBottomDrawerHeight(startHeight + delta);
      setShellUi((prev) =>
        kind === "trade"
          ? { ...prev, tradeDrawerHeight: nextHeight }
          : { ...prev, backtestDrawerHeight: nextHeight },
      );
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  const activeInstrument = telemetry.instrumentId || primarySnap.instrumentId;
  const activeTimeframe = telemetry.timeframe || primarySnap.timeframe;
  const activePrecision =
    instruments.find((item) => item.instrument_id === activeInstrument)
      ?.price_precision ?? 2;
  const allStructures = useMemo(
    () =>
      structuresController.structures.filter(
        (item) => !structuresController.ignoredIds.has(item.structure_id),
      ),
    [structuresController.structures, structuresController.ignoredIds],
  );
  const filteredStructures = useMemo(
    () =>
      visibleStructures(
        structuresController.structures,
        structureFilter,
        structuresController.ignoredIds,
      ),
    [structuresController.structures, structureFilter, structuresController.ignoredIds],
  );

  const activeDrawingsCount = useMemo(
    () => drawings.filter((d) => d.deleted_at == null).length,
    [drawings],
  );
  const layersTotalCount =
    filteredStructures.length +
    activeDrawingsCount +
    annotationsController.annotations.length;
  const monitorBadgeCount =
    alertsController.unread > 0
      ? alertsController.unread
      : scannerController.triggers.length;

  const activeRightDrawer: RightDrawerId | null =
    shellUi.mode === "live"
      ? shellUi.rightDrawer
      : shellUi.rightDrawer === "layers"
        ? "layers"
        : null;

  const renderLayersPanel = () => (
    <LayersPanel
      sourceFilter={shellUi.layerSource}
      onSourceChange={(layerSource) =>
        setShellUi((prev) => ({ ...prev, layerSource }))
      }
      structureProps={{
        structures: filteredStructures,
        allStructures,
        activeTimeframe,
        filter: structureFilter,
        onFilterChange: onStructureFilterChange,
        precision: activePrecision,
        ignoredCount: structuresController.ignoredIds.size,
        onIgnore: structuresController.ignore,
        ignoringId: structuresController.ignoringId,
        onAdopt: (feature) => void onAdoptStructure(feature),
        adoptingId: structuresController.adoptingId,
        adoptedIds: structuresController.adoptedIds,
        onAttachAlert: (feature) => {
          onSeedAlert({
            price: structureMidPrice(feature),
            instrumentId: activeInstrument,
            timeframe: activeTimeframe,
          });
          onOpenRightTab("alerts");
        },
        loading: structuresController.loading,
        error: structuresController.error,
        readOnly,
        notes: structuresController.notes,
        highlightedId: highlightedStructureId,
        onHover: setHighlightedStructureId,
      }}
      drawingProps={{
        drawings,
        selectedId: selectedDrawingId,
        readOnly: readOnly || replayActive,
        onSelect: setSelectedDrawingId,
        onDelete: (id) => void onDeleteDrawing(id),
        onAttachAlert: (drawing) => {
          const price = drawing.points[0]?.price
            ? Number(drawing.points[0].price)
            : null;
          onSeedAlert({
            drawingId: drawing.drawing_id,
            price,
            instrumentId: drawing.instrument_id,
            timeframe: activeTimeframe,
          });
          onOpenRightTab("alerts");
        },
        onClearInstrument: () => void onClearDrawings(),
      }}
      annotationProps={{
        controller: annotationsController,
        readOnly: readOnly || replayActive,
        onAdopted: () => primaryLink.notifyDrawingsChanged(activeInstrument),
      }}
    />
  );

  const renderMonitorPanel = () => (
    <MonitorPanel
      activeTab={shellUi.monitorTab}
      onTabChange={(monitorTab) =>
        setShellUi((prev) => ({ ...prev, monitorTab }))
      }
      scannerController={scannerController}
      alertsController={alertsController}
      deniedAlerts={deniedAlerts}
      instrumentId={activeInstrument}
      timeframe={activeTimeframe}
      instruments={instruments}
      drawings={drawings}
      link={primaryLink}
      alertSeed={alertSeed}
      onSeedConsumed={clearAlertSeed}
      onLocate={onLocateTarget}
      readOnly={readOnly}
      replayActive={replayActive}
    />
  );

  const renderTradePanel = (showSplitToggle: boolean, onCollapse?: () => void) => (
    <TradePanel
      activeTab={shellUi.tradeTab}
      onTabChange={(tradeTab) => setShellUi((prev) => ({ ...prev, tradeTab }))}
      showSplitToggle={showSplitToggle}
      onCollapse={onCollapse}
      draftPanelProps={{
        activeInstrumentId: activeInstrument,
        activeTimeframe,
        readOnly,
        replayActive,
        externalSeed: draftSeed,
        onExternalSeedConsumed: clearDraftSeed,
        highlightedDraftId: selectedDraftId,
        onDraftsMutated: notifyDraftsChanged,
      }}
    />
  );

  return (
    <WorkbenchProvider value={contextValue}>
      <div
        className={`talaria-workbench${popoutConfig ? " is-popout" : ""}${
          isMobile ? " is-mobile" : ""
        }${readOnly ? " is-readonly" : ""}`}
        data-theme={theme}
        data-testid="talaria-workbench"
      >
        <header className="wb-header">
          <div className="wb-brand">
            <span className="wb-logo">T</span>
            <div>
              <h1>{t("Talaria 看盘工作台", "Talaria Workbench")}</h1>
              <span className="wb-subtitle">
                {popoutConfig
                  ? t("独立弹窗 · 跨窗联动", "Popout Window · BroadcastChannel Sync")
                  : `${telemetry.instrumentId} · ${telemetry.timeframe}`}
                {!popoutConfig && activeDataDir && (
                  <>
                    {" · "}
                    <span
                      className="wb-data-dir"
                      data-testid="workbench-data-dir"
                      title={activeDataDir}
                    >
                      {activeDataDir}
                    </span>
                  </>
                )}
              </span>
            </div>
            {!popoutConfig && !isMobile && (
              <div
                className="wb-mode-switch"
                role="tablist"
                aria-label={t("工作台模式", "Workspace mode")}
              >
                <button
                  type="button"
                  role="tab"
                  aria-selected={shellUi.mode === "live"}
                  className={`wb-mode-btn${shellUi.mode === "live" ? " is-active" : ""}`}
                  data-testid="workspace-mode-live"
                  onClick={() =>
                    setShellUi((prev) => ({ ...prev, mode: "live" }))
                  }
                >
                  {consolidatedLabel("live", locale)}
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={shellUi.mode === "backtest"}
                  className={`wb-mode-btn${shellUi.mode === "backtest" ? " is-active" : ""}`}
                  data-testid="workspace-mode-backtest"
                  onClick={() =>
                    setShellUi((prev) => ({
                      ...prev,
                      mode: "backtest",
                      backtestDrawerOpen: true,
                    }))
                  }
                >
                  {consolidatedLabel("backtest", locale)}
                </button>
              </div>
            )}
          </div>

          <div className="wb-toolbar">
            {!popoutConfig && !isMobile && (
              <>
                <div className="wb-btn-group" role="group" aria-label={t("布局预设", "Layout presets")}>
                  <button
                    type="button"
                    className="ghost-button"
                    data-testid="preset-single"
                    onClick={() => applyPreset("single")}
                  >
                    1×1
                  </button>
                  <button
                    type="button"
                    className="ghost-button"
                    data-testid="preset-multi-tf"
                    onClick={() => applyPreset("multi-tf")}
                  >
                    <LayoutGrid size={13} /> 1m+5m+1h
                  </button>
                  <button
                    type="button"
                    className="ghost-button"
                    data-testid="preset-quad"
                    onClick={() => applyPreset("quad")}
                  >
                    2×2
                  </button>
                </div>

                <button
                  type="button"
                  className="ghost-button"
                  data-testid="add-chart-pane"
                  onClick={addChartPane}
                >
                  <Plus size={14} /> {t("加图表", "Chart")}
                </button>

                <button
                  type="button"
                  className="ghost-button"
                  data-testid="popout-active"
                  onClick={() =>
                    onPopoutChart({
                      paneId: telemetry.paneId,
                      instrumentId: telemetry.instrumentId,
                      timeframe: telemetry.timeframe,
                    })
                  }
                >
                  <ExternalLink size={14} /> {t("弹窗", "Popout")}
                </button>

                <button
                  type="button"
                  className="ghost-button"
                  data-testid="reset-layout"
                  onClick={resetLayout}
                  title={t("重置布局", "Reset layout")}
                >
                  <RotateCcw size={14} />
                </button>
              </>
            )}

            {!popoutConfig && isMobile && (
              <button
                type="button"
                className={`ghost-button${mobileDrawerOpen ? " is-active" : ""}`}
                data-testid="toggle-right-dock"
                onClick={() => setMobileDrawerOpen((v) => !v)}
              >
                <PanelRight size={14} /> {t("面板", "Panels")}
              </button>
            )}

            {tauriKeepRunning !== null && (
              <button
                type="button"
                className={`ghost-button${tauriKeepRunning ? " is-active" : ""}`}
                data-testid="tauri-bg-toggle"
                onClick={() => {
                  const next = !tauriKeepRunning;
                  setTauriKeepRunning(next);
                  void setTauriKeepRunningInBackground(next);
                }}
              >
                {tauriKeepRunning
                  ? t("关窗保留采集", "Keep daemon on close")
                  : t("关窗停服", "Stop daemon on close")}
              </button>
            )}

            <button
              type="button"
              className="ghost-button"
              data-testid="locale-toggle"
              onClick={toggleLocale}
            >
              {locale === "zh" ? "EN" : "中文"}
            </button>

            <button
              type="button"
              className="ghost-button"
              data-testid="theme-toggle"
              aria-label={
                theme === "dark"
                  ? t("切换浅色主题", "Switch to light theme")
                  : t("切换深色主题", "Switch to dark theme")
              }
              onClick={toggleTheme}
            >
              {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
              <span>{theme === "dark" ? t("浅色", "Light") : t("深色", "Dark")}</span>
            </button>
          </div>
        </header>

        {budget.exceeded && (
          <div className="webgl-budget-banner" role="status" data-testid="webgl-budget-notice">
            <strong>
              {t("WebGL2 预算超限提示", "WebGL2 Budget Notice")}:
            </strong>{" "}
            {t(
              `当前活跃图表 ${budget.activeChartCount} 个（上限 ${budget.maxWebglCells} 个），超出图表 (${budget.canvas2dFallbackCount} 个) 已自动降级为 Canvas2D 保持一致渲染。`,
              `${budget.activeChartCount} active charts exceed the ${budget.maxWebglCells}-cell WebGL2 budget; ${budget.canvas2dFallbackCount} pane(s) degraded cleanly to Canvas2D.`,
            )}
          </div>
        )}

        <main className="wb-main">
          {popoutConfig ? (
            <div className="wb-popout-stage">
              <ChartPane
                paneId={popoutConfig.paneId}
                initialTimeframe={popoutConfig.timeframe}
                syncTimeframe
                isPopout
              />
            </div>
          ) : isMobile ? (
            <div className="wb-mobile-stage">
              <ChartPane
                paneId="chart-mobile"
                initialTimeframe={primarySnap.timeframe}
                syncTimeframe
              />
              {mobileDrawerOpen && (
                <div className="wb-mobile-drawer" data-testid="mobile-drawer">
                  <div className="wb-mobile-drawer-tabs">
                    {MOBILE_TABS.map((tabId) => (
                      <button
                        key={tabId}
                        type="button"
                        className={`tab-button${mobileTab === tabId ? " is-active" : ""}`}
                        onClick={() => setMobileTab(tabId)}
                      >
                        {consolidatedLabel(tabId, locale)}
                      </button>
                    ))}
                  </div>
                  <div className="wb-mobile-drawer-body">
                    {mobileTab === "watchlist" && (
                      <WatchlistPanel
                        instruments={instruments}
                        selectedInstrumentId={activeInstrument}
                        onSelect={onSelectInstrument}
                        onOpenSymbolSearch={onOpenSymbolSearch}
                      />
                    )}
                    {mobileTab === "monitor" && renderMonitorPanel()}
                    {mobileTab === "layers" && renderLayersPanel()}
                    {mobileTab === "trade" && renderTradePanel(false)}
                    {mobileTab === "runs" && (
                      <RunsPanel
                        controller={runsController}
                        activeTimeframe={activeTimeframe}
                        onJumpToRound={onJumpToRound}
                        onReplayBeforeRound={onReplayBeforeRound}
                      />
                    )}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className={`wb-workspace-body is-mode-${shellUi.mode}`}>
              <div className="wb-center-column">
                <div className="wb-dockview-host" data-testid="dockview-host">
                  <DockviewReact
                    theme={theme === "dark" ? themeDark : themeLight}
                    components={DOCKVIEW_COMPONENTS}
                    onReady={handleDockReady}
                  />
                </div>

                {shellUi.mode === "live" && shellUi.tradeDrawerOpen && (
                  <>
                    <div
                      className="wb-resize-handle-y"
                      role="separator"
                      aria-orientation="horizontal"
                      onPointerDown={(e) => startResizeBottomDrawer(e, "trade")}
                    />
                    <section
                      className="wb-bottom-drawer"
                      style={{ height: shellUi.tradeDrawerHeight }}
                      data-testid="trade-bottom-drawer"
                    >
                      {renderTradePanel(true, () =>
                        setShellUi((prev) => ({ ...prev, tradeDrawerOpen: false })),
                      )}
                    </section>
                  </>
                )}

                {shellUi.mode === "backtest" && shellUi.backtestDrawerOpen && (
                  <>
                    <div
                      className="wb-resize-handle-y"
                      role="separator"
                      aria-orientation="horizontal"
                      onPointerDown={(e) => startResizeBottomDrawer(e, "backtest")}
                    />
                    <section
                      className="wb-bottom-drawer wb-bottom-drawer--backtest"
                      style={{ height: shellUi.backtestDrawerHeight }}
                      data-testid="backtest-bottom-drawer"
                    >
                      <RunsPanel
                        controller={runsController}
                        activeTimeframe={activeTimeframe}
                        onJumpToRound={onJumpToRound}
                        onReplayBeforeRound={onReplayBeforeRound}
                        layout="wide"
                        onCollapse={() =>
                          setShellUi((prev) => ({ ...prev, backtestDrawerOpen: false }))
                        }
                      />
                    </section>
                  </>
                )}
              </div>

              {activeRightDrawer && (
                <>
                  <div
                    className="wb-resize-handle-x"
                    role="separator"
                    aria-orientation="vertical"
                    onPointerDown={startResizeRightDrawer}
                  />
                  <aside
                    className="wb-right-drawer"
                    style={{ width: shellUi.rightDrawerWidth }}
                    data-testid={`right-drawer-${activeRightDrawer}`}
                  >
                    {activeRightDrawer === "watchlist" && (
                      <WatchlistPanel
                        instruments={instruments}
                        selectedInstrumentId={activeInstrument}
                        onSelect={onSelectInstrument}
                        onOpenSymbolSearch={onOpenSymbolSearch}
                      />
                    )}
                    {activeRightDrawer === "monitor" && renderMonitorPanel()}
                    {activeRightDrawer === "layers" && renderLayersPanel()}
                  </aside>
                </>
              )}

              <nav
                className="wb-activity-rail"
                aria-label={t("工作台功能栏", "Workbench activity rail")}
                data-testid="activity-rail"
              >
                {shellUi.mode === "live" && (
                  <>
                    <button
                      type="button"
                      className={`wb-rail-btn${
                        activeRightDrawer === "watchlist" ? " is-active" : ""
                      }`}
                      data-testid="rail-watchlist"
                      aria-pressed={activeRightDrawer === "watchlist"}
                      onClick={() => toggleRightRail("watchlist")}
                    >
                      <Bookmark size={16} />
                      <span>{consolidatedLabel("watchlist", locale)}</span>
                    </button>

                    <button
                      type="button"
                      className={`wb-rail-btn${
                        activeRightDrawer === "monitor" ? " is-active" : ""
                      }`}
                      data-testid="rail-monitor"
                      aria-pressed={activeRightDrawer === "monitor"}
                      onClick={() => toggleRightRail("monitor")}
                    >
                      <Activity size={16} />
                      <span>{consolidatedLabel("monitor", locale)}</span>
                      {monitorBadgeCount > 0 && (
                        <span
                          className={`wb-rail-badge${
                            alertsController.unread > 0 ? " is-alert" : ""
                          }`}
                        >
                          {monitorBadgeCount}
                        </span>
                      )}
                    </button>
                  </>
                )}

                <button
                  type="button"
                  className={`wb-rail-btn${
                    activeRightDrawer === "layers" ? " is-active" : ""
                  }`}
                  data-testid="rail-layers"
                  aria-pressed={activeRightDrawer === "layers"}
                  onClick={() => toggleRightRail("layers")}
                >
                  <Layers size={16} />
                  <span>{consolidatedLabel("layers", locale)}</span>
                  {layersTotalCount > 0 && (
                    <span className="wb-rail-badge">{layersTotalCount}</span>
                  )}
                </button>
              </nav>
            </div>
          )}
        </main>

        <footer className="wb-footer" data-testid="status-bar">
          <div className="wb-footer-left">
            <span data-testid="feed-mode">
              {feedMode === "replay"
                ? t("回放模式", "Replay")
                : feedMode === "bybit"
                  ? t("Bybit 实时", "Bybit Live")
                  : t("数据源", "Feed") + `: ${feedMode ?? "—"}`}
            </span>
            <span className="status-sep">·</span>
            <span data-testid="loaded-count">
              {t(`已加载 ${telemetry.stats.loaded} 根`, `${telemetry.stats.loaded} bars`)}
            </span>
            <span className="status-sep">·</span>
            <span data-testid="gap-count">
              {t(`断流 ${telemetry.stats.gaps} 处`, `${telemetry.stats.gaps} gaps`)}
            </span>
            <span className="status-sep">·</span>
            <span data-testid="webgl-budget-count">
              WebGL2 {budget.webglCount}/{budget.maxWebglCells}
              {budget.canvas2dFallbackCount > 0
                ? ` (+${budget.canvas2dFallbackCount} Canvas2D)`
                : ""}
            </span>
          </div>

          <div className="wb-footer-right">
            {!popoutConfig && !isMobile && shellUi.mode === "live" && (
              <button
                type="button"
                className={`wb-footer-drawer-toggle${
                  shellUi.tradeDrawerOpen ? " is-active" : ""
                }`}
                data-testid="footer-trade-toggle"
                onClick={() =>
                  setShellUi((prev) => ({
                    ...prev,
                    tradeDrawerOpen: !prev.tradeDrawerOpen,
                  }))
                }
              >
                <Briefcase size={12} />
                <span>{consolidatedLabel("trade", locale)}</span>
                {shellUi.tradeDrawerOpen ? (
                  <ChevronDown size={12} />
                ) : (
                  <ChevronUp size={12} />
                )}
              </button>
            )}

            {!popoutConfig && !isMobile && shellUi.mode === "backtest" && (
              <button
                type="button"
                className={`wb-footer-drawer-toggle${
                  shellUi.backtestDrawerOpen ? " is-active" : ""
                }`}
                data-testid="footer-backtest-toggle"
                onClick={() =>
                  setShellUi((prev) => ({
                    ...prev,
                    backtestDrawerOpen: !prev.backtestDrawerOpen,
                  }))
                }
              >
                <History size={12} />
                <span>
                  {consolidatedLabel("runs", locale)} ({runsController.runs.length})
                </span>
                {shellUi.backtestDrawerOpen ? (
                  <ChevronDown size={12} />
                ) : (
                  <ChevronUp size={12} />
                )}
              </button>
            )}

            <div className="wb-vela-group" data-testid="vela-attribution">
              <span>{t("图表引擎：", "Charted by ")}</span>
              <a href={VELA_ATTRIBUTION.url} target="_blank" rel="noreferrer">
                {VELA_ATTRIBUTION.label}
              </a>
              <span className="wb-attribution-url">({VELA_ATTRIBUTION.url})</span>
            </div>
          </div>
        </footer>
        {!replayActive && alertsController.toast && (
          <AlertToast
            trigger={alertsController.toast}
            onLocate={(trig) => {
              onLocateTarget(trig);
              alertsController.clearToast();
            }}
            onDismiss={alertsController.clearToast}
          />
        )}
        <InstrumentSearchModal
          open={symbolSearch.open}
          title={symbolSearch.options?.title}
          actionLabel={symbolSearch.options?.actionLabel}
          currentInstrumentId={
            symbolSearch.options?.currentInstrumentId ?? telemetry.instrumentId
          }
          existingInstruments={instruments}
          onClose={() => setSymbolSearch({ open: false })}
          onInstrumentsChanged={setInstruments}
          onPicked={async (spec) => {
            if (symbolSearch.options?.onPicked) {
              await symbolSearch.options.onPicked(spec);
            } else {
              onSelectInstrument(spec.instrument_id);
            }
          }}
        />
      </div>
    </WorkbenchProvider>
  );
}
