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
  ExternalLink,
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
} from "@talaria/contracts";

import { VELA_ATTRIBUTION } from "../chart/adapter";
import { useWebglBudget } from "../chart/webglBudget";
import { AlertWorkbench } from "../features/alerts/AlertWorkbench";
import type { AlertFormSeed } from "../features/alerts/AlertPanel";
import { useAlerts } from "../features/alerts/useAlerts";
import { AnnotationPanel } from "../features/annotations/AnnotationPanel";
import { useAnnotations } from "../features/annotations/useAnnotations";
import { DrawingManager } from "../features/drawings/DrawingManager";
import {
  isMobileDrawerViewport,
  isReadOnlyViewport,
} from "../features/drawings/responsive";
import {
  INDICATOR_DEFAULTS,
  defaultIndicatorSettings,
  defaultsFromParams,
  operatorParams,
  updateIndicatorSetting,
  type IndicatorDefaults,
  type IndicatorSetting,
  type IndicatorSettings,
} from "../features/indicators/config";
import { KeyLevelPanel } from "../features/key-levels/KeyLevelPanel";
import {
  defaultKeyLevelFilter,
  visibleKeyLevels,
  type KeyLevelFilter,
} from "../features/key-levels/model";
import { useKeyLevels } from "../features/key-levels/useKeyLevels";
import { ScannerPanel } from "../features/scanner/ScannerPanel";
import { useScanner } from "../features/scanner/useScanner";
import { WatchlistPanel } from "../features/watchlist/WatchlistPanel";
import { api } from "../lib/api";
import { getLocale, setLocale, t, useLocale } from "../lib/i18n";
import {
  getTauriShellSettings,
  isTauriRuntime,
  openTauriPopoutWindow,
  setTauriKeepRunningInBackground,
} from "../lib/tauriBridge";
import { applyTheme, THEME_STORAGE_KEY, type Theme } from "../lib/theme";
import { ChartPane } from "./ChartPane";
import {
  applyPresetLayout,
  clearSavedLayout,
  loadLayoutFromStorage,
  saveLayoutToStorage,
  type ChartPaneParams,
  type LayoutPreset,
} from "./layoutStore";
import type { LocateTarget } from "./link";
import { DESK_PATH, panelLabel, type PanelName } from "./panels";
import { useLinkSnapshot, usePanelLink } from "./usePanelLink";
import {
  WorkbenchProvider,
  useWorkbenchContext,
  type ActiveChartTelemetry,
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

function DockWatchlistPanel() {
  const ctx = useWorkbenchContext();
  return (
    <WatchlistPanel
      instruments={ctx.instruments}
      selectedInstrumentId={ctx.activeInstrumentId}
      onSelect={ctx.onSelectInstrument}
    />
  );
}

function DockAlertsPanel() {
  const ctx = useWorkbenchContext();
  return (
    <AlertWorkbench
      instrumentId={ctx.activeInstrumentId}
      timeframe={ctx.activeTimeframe}
      instruments={ctx.instruments}
      onLocate={ctx.onLocateTarget}
      link={ctx.primaryLink}
      formSeed={ctx.alertSeed}
    />
  );
}

function DockKeyLevelsPanel() {
  const ctx = useWorkbenchContext();
  const precision =
    ctx.instruments.find((item) => item.instrument_id === ctx.activeInstrumentId)
      ?.price_precision ?? 2;
  const allLevels = ctx.keyLevelsController.levels.filter(
    (lvl) => !ctx.keyLevelsController.ignoredIds.has(lvl.level_id),
  );
  const filtered = visibleKeyLevels(
    ctx.keyLevelsController.levels,
    ctx.keyLevelFilter,
    ctx.keyLevelsController.ignoredIds,
  );
  return (
    <KeyLevelPanel
      levels={filtered}
      allLevels={allLevels}
      filter={ctx.keyLevelFilter}
      onFilterChange={ctx.onKeyLevelFilterChange}
      precision={precision}
      ignoredCount={ctx.keyLevelsController.ignoredIds.size}
      onIgnore={ctx.keyLevelsController.ignore}
      ignoringId={ctx.keyLevelsController.ignoringId}
      onAdopt={(level) => void ctx.onAdoptKeyLevel(level)}
      adoptingId={ctx.keyLevelsController.adoptingId}
      adoptedIds={ctx.keyLevelsController.adoptedIds}
      onAttachAlert={(level) => {
        ctx.onSeedAlert({
          levelId: level.level_id,
          levelKind: level.kind,
          instrumentId: ctx.activeInstrumentId,
          timeframe: ctx.activeTimeframe,
        });
        ctx.onOpenRightTab("alerts");
      }}
      loading={ctx.keyLevelsController.loading}
      error={ctx.keyLevelsController.error}
      readOnly={ctx.readOnly}
      notes={ctx.keyLevelsController.notes}
      highlightedId={ctx.highlightedLevelId}
      onHover={ctx.onHoverLevelId}
    />
  );
}

function DockScannerPanel() {
  const ctx = useWorkbenchContext();
  return (
    <ScannerPanel
      controller={ctx.scannerController}
      onLocate={ctx.onLocateTarget}
      readOnly={ctx.readOnly}
    />
  );
}

function DockDrawingsPanel() {
  const ctx = useWorkbenchContext();
  return (
    <DrawingManager
      drawings={ctx.drawings}
      selectedId={ctx.selectedDrawingId}
      readOnly={ctx.readOnly}
      onSelect={ctx.onDrawingSelected}
      onDelete={(id) => void ctx.onDeleteDrawing(id)}
      onAttachAlert={(drawing) => {
        const price = drawing.points[0]?.price ? Number(drawing.points[0].price) : null;
        ctx.onSeedAlert({
          drawingId: drawing.drawing_id,
          price,
          instrumentId: drawing.instrument_id,
          timeframe: ctx.activeTimeframe,
        });
        ctx.onOpenRightTab("alerts");
      }}
      onClearInstrument={() => void ctx.onClearDrawings()}
    />
  );
}

function DockAnnotationsPanel() {
  const ctx = useWorkbenchContext();
  return (
    <AnnotationPanel
      controller={ctx.annotationsController}
      readOnly={ctx.readOnly}
      onAdopted={() => ctx.primaryLink.notifyDrawingsChanged(ctx.activeInstrumentId)}
    />
  );
}

const DOCKVIEW_COMPONENTS = {
  chart: DockChartPanel,
  watchlist: DockWatchlistPanel,
  alerts: DockAlertsPanel,
  levels: DockKeyLevelsPanel,
  scanner: DockScannerPanel,
  drawings: DockDrawingsPanel,
  annotations: DockAnnotationsPanel,
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

const RIGHT_DOCK_TABS = [
  "watchlist",
  "alerts",
  "levels",
  "scanner",
  "drawings",
  "annotations",
] as const;

type RightTabId = (typeof RIGHT_DOCK_TABS)[number];

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
  const [indicatorSettings, setIndicatorSettings] = useState<IndicatorSettings>(() =>
    defaultIndicatorSettings(INDICATOR_DEFAULTS),
  );
  const [savingIndicatorDefault, setSavingIndicatorDefault] =
    useState<IndicatorName | null>(null);

  const [keyLevelFilter, setKeyLevelFilter] =
    useState<KeyLevelFilter>(defaultKeyLevelFilter);
  const [highlightedLevelId, setHighlightedLevelId] = useState<string | null>(null);
  const [drawings, setDrawings] = useState<DrawingRecord[]>([]);
  const [selectedDrawingId, setSelectedDrawingId] = useState<string | null>(null);
  const [alertSeed, setAlertSeed] = useState<AlertFormSeed | null>(null);

  const [rightDockVisible, setRightDockVisible] = useState(true);
  const [mobileDrawerOpen, setMobileDrawerOpen] = useState(false);
  const [mobileTab, setMobileTab] = useState<RightTabId>("watchlist");
  const [tauriKeepRunning, setTauriKeepRunning] = useState<boolean | null>(null);

  const dockApiRef = useRef<DockviewApi | null>(null);
  const saveTimerRef = useRef<number | null>(null);
  const extraPaneCounterRef = useRef(1);

  const budget = useWebglBudget();
  const alertsController = useAlerts();
  const scannerController = useScanner(true);
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

  useEffect(() => {
    const onResize = () => setViewportWidth(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
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
        setIndicatorDefaults(defs);
        setIndicatorSettings(defaultIndicatorSettings(defs));
      })
      .catch(() => undefined);

    if (isTauriRuntime()) {
      void getTauriShellSettings().then((settings) => {
        if (active && settings) {
          setTauriKeepRunning(settings.keep_running_in_background);
        }
      });
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
      setIndicatorSettings((prev) =>
        updateIndicatorSetting(prev, indicatorDefaults, name, patch),
      );
    },
    [indicatorDefaults],
  );

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

  const onSeedAlert = useCallback((seed: Omit<AlertFormSeed, "nonce">) => {
    setAlertSeed({ ...seed, nonce: Date.now() });
  }, []);

  const onOpenRightTab = useCallback(
    (tab: RightTabId) => {
      if (isMobile) {
        setMobileTab(tab);
        setMobileDrawerOpen(true);
        return;
      }
      const dockApi = dockApiRef.current;
      const panel = dockApi?.getPanel(`dock-${tab}`);
      if (panel) {
        panel.group.api.setVisible(true);
        panel.api.setActive();
        setRightDockVisible(true);
      }
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
      onKeyLevelFilterChange: setKeyLevelFilter,
      highlightedLevelId,
      onHoverLevelId: setHighlightedLevelId,
      keyLevelsController,
      drawings,
      selectedDrawingId,
      onDrawingsUpdated: setDrawings,
      onDrawingSelected: setSelectedDrawingId,
      onDeleteDrawing,
      onClearDrawings,
      alertsController,
      alertSeed,
      onSeedAlert,
      scannerController,
      annotationsController,
      onSelectInstrument,
      onLocateTarget,
      onPopoutChart,
      onOpenRightTab,
      onAdoptKeyLevel,
      onAdoptAnnotation,
    }),
    [
      theme,
      locale,
      readOnly,
      instruments,
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
      highlightedLevelId,
      keyLevelsController,
      drawings,
      selectedDrawingId,
      onDeleteDrawing,
      onClearDrawings,
      alertsController,
      alertSeed,
      onSeedAlert,
      scannerController,
      annotationsController,
      onSelectInstrument,
      onLocateTarget,
      onPopoutChart,
      onOpenRightTab,
      onAdoptKeyLevel,
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
    setRightDockVisible(true);
  };

  const resetLayout = () => {
    clearSavedLayout();
    applyPreset("multi-tf");
  };

  const toggleRightDock = () => {
    if (isMobile) {
      setMobileDrawerOpen((v) => !v);
      return;
    }
    const dockApi = dockApiRef.current;
    const watchlistPanel = dockApi?.getPanel("dock-watchlist");
    if (watchlistPanel) {
      const next = !rightDockVisible;
      watchlistPanel.group.api.setVisible(next);
      setRightDockVisible(next);
    }
  };

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
              </span>
            </div>
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

            {!popoutConfig && (
              <button
                type="button"
                className={`ghost-button${
                  isMobile ? (mobileDrawerOpen ? " is-active" : "") : rightDockVisible ? " is-active" : ""
                }`}
                data-testid="toggle-right-dock"
                onClick={toggleRightDock}
              >
                <PanelRight size={14} />{" "}
                {isMobile ? t("面板", "Panels") : t("右栏", "Dock")}
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
                    {RIGHT_DOCK_TABS.map((tabId) => (
                      <button
                        key={tabId}
                        type="button"
                        className={`tab-button${mobileTab === tabId ? " is-active" : ""}`}
                        onClick={() => setMobileTab(tabId)}
                      >
                        {panelLabel(tabId, locale)}
                      </button>
                    ))}
                  </div>
                  <div className="wb-mobile-drawer-body">
                    {mobileTab === "watchlist" && <DockWatchlistPanel />}
                    {mobileTab === "alerts" && <DockAlertsPanel />}
                    {mobileTab === "levels" && <DockKeyLevelsPanel />}
                    {mobileTab === "scanner" && <DockScannerPanel />}
                    {mobileTab === "drawings" && <DockDrawingsPanel />}
                    {mobileTab === "annotations" && <DockAnnotationsPanel />}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className="wb-dockview-host" data-testid="dockview-host">
              <DockviewReact
                theme={theme === "dark" ? themeDark : themeLight}
                components={DOCKVIEW_COMPONENTS}
                onReady={handleDockReady}
              />
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

          <div className="wb-footer-right" data-testid="vela-attribution">
            <span>{t("图表引擎：", "Charted by ")}</span>
            <a href={VELA_ATTRIBUTION.url} target="_blank" rel="noreferrer">
              {VELA_ATTRIBUTION.label}
            </a>
            <span className="wb-attribution-url">({VELA_ATTRIBUTION.url})</span>
          </div>
        </footer>
      </div>
    </WorkbenchProvider>
  );
}
