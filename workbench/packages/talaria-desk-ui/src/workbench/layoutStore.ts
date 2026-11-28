import type { DockviewApi, SerializedDockview } from "dockview-react";

export const WORKBENCH_LAYOUT_STORAGE_KEY = "talaria-workbench-dockview-layout-v2";
export const SHELL_UI_STORAGE_KEY = "talaria-workbench-shell-ui-v1";

export type LayoutPreset = "multi-tf" | "single" | "quad";
export type WorkspaceMode = "live" | "backtest";
export type RightDrawerId = "watchlist" | "monitor" | "layers";
export type MonitorSubTab = "scanner" | "alerts";
export type LayerSourceTab = "all" | "structure" | "drawing" | "annotation";
export type TradeDrawerTab = "account" | "drafts" | "split";

export const MOBILE_TABS = ["watchlist", "monitor", "layers", "trade", "runs"] as const;
export type MobileTabId = (typeof MOBILE_TABS)[number];

export type LegacyOrConsolidatedPanelTarget =
  | "watchlist"
  | "alerts"
  | "levels"
  | "scanner"
  | "drawings"
  | "annotations"
  | "runs"
  | "drafts"
  | "account"
  | "monitor"
  | "layers"
  | "trade";

export interface ChartPaneParams {
  paneId: string;
  initialTimeframe: string;
  syncTimeframe: boolean;
}

export interface ShellUiState {
  mode: WorkspaceMode;
  rightDrawer: RightDrawerId | null;
  rightDrawerWidth: number;
  monitorTab: MonitorSubTab;
  layerSource: LayerSourceTab;
  tradeDrawerOpen: boolean;
  tradeDrawerHeight: number;
  tradeTab: TradeDrawerTab;
  backtestDrawerOpen: boolean;
  backtestDrawerHeight: number;
}

export const DEFAULT_SHELL_UI_STATE: ShellUiState = {
  mode: "live",
  rightDrawer: "watchlist",
  rightDrawerWidth: 340,
  monitorTab: "scanner",
  layerSource: "all",
  tradeDrawerOpen: false,
  tradeDrawerHeight: 280,
  tradeTab: "account",
  backtestDrawerOpen: true,
  backtestDrawerHeight: 320,
};

export function clampRightDrawerWidth(width: number): number {
  if (!Number.isFinite(width)) return DEFAULT_SHELL_UI_STATE.rightDrawerWidth;
  return Math.max(260, Math.min(560, Math.round(width)));
}

export function clampBottomDrawerHeight(height: number): number {
  if (!Number.isFinite(height)) return DEFAULT_SHELL_UI_STATE.tradeDrawerHeight;
  return Math.max(180, Math.min(560, Math.round(height)));
}

export function toggleRightDrawerState(
  state: ShellUiState,
  id: RightDrawerId,
): ShellUiState {
  return {
    ...state,
    rightDrawer: state.rightDrawer === id ? null : id,
  };
}

export function resolvePanelNavigation(
  state: ShellUiState,
  target: LegacyOrConsolidatedPanelTarget,
): ShellUiState {
  switch (target) {
    case "watchlist":
      return { ...state, mode: "live", rightDrawer: "watchlist" };
    case "alerts":
      return { ...state, mode: "live", rightDrawer: "monitor", monitorTab: "alerts" };
    case "scanner":
      return { ...state, mode: "live", rightDrawer: "monitor", monitorTab: "scanner" };
    case "monitor":
      return { ...state, mode: "live", rightDrawer: "monitor" };
    case "levels":
      return { ...state, rightDrawer: "layers", layerSource: "structure" };
    case "drawings":
      return { ...state, rightDrawer: "layers", layerSource: "drawing" };
    case "annotations":
      return { ...state, rightDrawer: "layers", layerSource: "annotation" };
    case "layers":
      return { ...state, rightDrawer: "layers" };
    case "drafts":
      return {
        ...state,
        mode: "live",
        tradeDrawerOpen: true,
        tradeTab: state.tradeTab === "split" ? "split" : "drafts",
      };
    case "account":
      return {
        ...state,
        mode: "live",
        tradeDrawerOpen: true,
        tradeTab: state.tradeTab === "split" ? "split" : "account",
      };
    case "trade":
      return { ...state, mode: "live", tradeDrawerOpen: true };
    case "runs":
      return { ...state, mode: "backtest", backtestDrawerOpen: true };
  }
}

export function resolveMobileNavigation(target: LegacyOrConsolidatedPanelTarget): {
  tab: MobileTabId;
  monitorTab?: MonitorSubTab;
  layerSource?: LayerSourceTab;
  tradeTab?: TradeDrawerTab;
} {
  switch (target) {
    case "watchlist":
      return { tab: "watchlist" };
    case "alerts":
      return { tab: "monitor", monitorTab: "alerts" };
    case "scanner":
      return { tab: "monitor", monitorTab: "scanner" };
    case "monitor":
      return { tab: "monitor" };
    case "levels":
      return { tab: "layers", layerSource: "structure" };
    case "drawings":
      return { tab: "layers", layerSource: "drawing" };
    case "annotations":
      return { tab: "layers", layerSource: "annotation" };
    case "layers":
      return { tab: "layers" };
    case "drafts":
      return { tab: "trade", tradeTab: "drafts" };
    case "account":
      return { tab: "trade", tradeTab: "account" };
    case "trade":
      return { tab: "trade" };
    case "runs":
      return { tab: "runs" };
  }
}

export function applyPresetLayout(
  api: DockviewApi,
  preset: LayoutPreset,
): void {
  api.clear();

  const p1 = api.addPanel<ChartPaneParams>({
    id: "chart-1m",
    component: "chart",
    title: "BTC-USDT-PERP · 1m",
    params: { paneId: "chart-1m", initialTimeframe: "1m", syncTimeframe: false },
  });

  if (preset === "multi-tf") {
    const p2 = api.addPanel<ChartPaneParams>({
      id: "chart-5m",
      component: "chart",
      title: "BTC-USDT-PERP · 5m",
      params: { paneId: "chart-5m", initialTimeframe: "5m", syncTimeframe: false },
      position: { referencePanel: p1, direction: "right" },
    });
    api.addPanel<ChartPaneParams>({
      id: "chart-1h",
      component: "chart",
      title: "BTC-USDT-PERP · 1h",
      params: { paneId: "chart-1h", initialTimeframe: "1h", syncTimeframe: false },
      position: { referencePanel: p2, direction: "below" },
    });
  } else if (preset === "quad") {
    const p2 = api.addPanel<ChartPaneParams>({
      id: "chart-5m",
      component: "chart",
      title: "BTC-USDT-PERP · 5m",
      params: { paneId: "chart-5m", initialTimeframe: "5m", syncTimeframe: false },
      position: { referencePanel: p1, direction: "right" },
    });
    api.addPanel<ChartPaneParams>({
      id: "chart-15m",
      component: "chart",
      title: "BTC-USDT-PERP · 15m",
      params: { paneId: "chart-15m", initialTimeframe: "15m", syncTimeframe: false },
      position: { referencePanel: p1, direction: "below" },
    });
    api.addPanel<ChartPaneParams>({
      id: "chart-1h",
      component: "chart",
      title: "BTC-USDT-PERP · 1h",
      params: { paneId: "chart-1h", initialTimeframe: "1h", syncTimeframe: false },
      position: { referencePanel: p2, direction: "below" },
    });
  }

  p1.api.setActive();
}

export function saveLayoutToStorage(
  api: DockviewApi,
  storage: Pick<Storage, "setItem"> | undefined = typeof localStorage !== "undefined"
    ? localStorage
    : undefined,
): void {
  if (!storage) return;
  try {
    storage.setItem(WORKBENCH_LAYOUT_STORAGE_KEY, JSON.stringify(api.toJSON()));
  } catch {
    // Ignore storage quota errors.
  }
}

export function loadLayoutFromStorage(
  storage: Pick<Storage, "getItem"> | undefined = typeof localStorage !== "undefined"
    ? localStorage
    : undefined,
): SerializedDockview | null {
  if (!storage) return null;
  try {
    const raw = storage.getItem(WORKBENCH_LAYOUT_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as SerializedDockview;
    if (
      parsed &&
      typeof parsed === "object" &&
      parsed.grid &&
      parsed.panels &&
      Object.keys(parsed.panels).length > 0
    ) {
      const panels = Object.values(parsed.panels) as Array<{ contentComponent?: string }>;
      if (panels.every((p) => p?.contentComponent === "chart")) {
        return parsed;
      }
    }
  } catch {
    // Corrupt layout -> fall back to preset.
  }
  return null;
}

export function clearSavedLayout(
  storage: Pick<Storage, "removeItem"> | undefined = typeof localStorage !== "undefined"
    ? localStorage
    : undefined,
): void {
  try {
    storage?.removeItem(WORKBENCH_LAYOUT_STORAGE_KEY);
  } catch {
    // Ignore.
  }
}

export function saveShellUiState(
  state: ShellUiState,
  storage: Pick<Storage, "setItem"> | undefined = typeof localStorage !== "undefined"
    ? localStorage
    : undefined,
): void {
  if (!storage) return;
  try {
    storage.setItem(SHELL_UI_STORAGE_KEY, JSON.stringify(state));
  } catch {
    // Ignore storage quota errors.
  }
}

export function loadShellUiState(
  storage: Pick<Storage, "getItem"> | undefined = typeof localStorage !== "undefined"
    ? localStorage
    : undefined,
): ShellUiState {
  if (!storage) return DEFAULT_SHELL_UI_STATE;
  try {
    const raw = storage.getItem(SHELL_UI_STORAGE_KEY);
    if (!raw) return DEFAULT_SHELL_UI_STATE;
    const parsed = JSON.parse(raw) as Partial<ShellUiState> | null;
    if (!parsed || typeof parsed !== "object") return DEFAULT_SHELL_UI_STATE;

    const mode: WorkspaceMode = parsed.mode === "backtest" ? "backtest" : "live";
    const rightDrawer: RightDrawerId | null =
      parsed.rightDrawer === null
        ? null
        : parsed.rightDrawer === "watchlist" ||
            parsed.rightDrawer === "monitor" ||
            parsed.rightDrawer === "layers"
          ? parsed.rightDrawer
          : DEFAULT_SHELL_UI_STATE.rightDrawer;
    const monitorTab: MonitorSubTab =
      parsed.monitorTab === "alerts" ? "alerts" : "scanner";
    const layerSource: LayerSourceTab =
      parsed.layerSource === "structure" ||
      parsed.layerSource === "drawing" ||
      parsed.layerSource === "annotation"
        ? parsed.layerSource
        : "all";
    const tradeTab: TradeDrawerTab =
      parsed.tradeTab === "drafts" || parsed.tradeTab === "split"
        ? parsed.tradeTab
        : "account";

    return {
      mode,
      rightDrawer,
      rightDrawerWidth: clampRightDrawerWidth(
        typeof parsed.rightDrawerWidth === "number"
          ? parsed.rightDrawerWidth
          : DEFAULT_SHELL_UI_STATE.rightDrawerWidth,
      ),
      monitorTab,
      layerSource,
      tradeDrawerOpen:
        typeof parsed.tradeDrawerOpen === "boolean"
          ? parsed.tradeDrawerOpen
          : DEFAULT_SHELL_UI_STATE.tradeDrawerOpen,
      tradeDrawerHeight: clampBottomDrawerHeight(
        typeof parsed.tradeDrawerHeight === "number"
          ? parsed.tradeDrawerHeight
          : DEFAULT_SHELL_UI_STATE.tradeDrawerHeight,
      ),
      tradeTab,
      backtestDrawerOpen:
        typeof parsed.backtestDrawerOpen === "boolean"
          ? parsed.backtestDrawerOpen
          : DEFAULT_SHELL_UI_STATE.backtestDrawerOpen,
      backtestDrawerHeight: clampBottomDrawerHeight(
        typeof parsed.backtestDrawerHeight === "number"
          ? parsed.backtestDrawerHeight
          : DEFAULT_SHELL_UI_STATE.backtestDrawerHeight,
      ),
    };
  } catch {
    return DEFAULT_SHELL_UI_STATE;
  }
}
