import type { DockviewApi, SerializedDockview } from "dockview-react";

import { t } from "../lib/i18n";

export const WORKBENCH_LAYOUT_STORAGE_KEY = "talaria-workbench-dockview-layout-v1";

export type LayoutPreset = "multi-tf" | "single" | "quad";

export interface ChartPaneParams {
  paneId: string;
  initialTimeframe: string;
  syncTimeframe: boolean;
}

export interface RightDockParams {
  panelType: "watchlist" | "alerts" | "levels" | "scanner" | "drawings" | "annotations";
}

export function applyPresetLayout(api: DockviewApi, preset: LayoutPreset): void {
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

  const rightDock = api.addPanel<RightDockParams>({
    id: "dock-watchlist",
    component: "watchlist",
    title: t("自选", "Watchlist"),
    params: { panelType: "watchlist" },
    position: { direction: "right" },
    initialWidth: 340,
  });

  api.addPanel<RightDockParams>({
    id: "dock-alerts",
    component: "alerts",
    title: t("预警", "Alerts"),
    params: { panelType: "alerts" },
    position: { referenceGroup: rightDock.group },
  });
  api.addPanel<RightDockParams>({
    id: "dock-levels",
    component: "levels",
    title: t("关键位", "Key Levels"),
    params: { panelType: "levels" },
    position: { referenceGroup: rightDock.group },
  });
  api.addPanel<RightDockParams>({
    id: "dock-scanner",
    component: "scanner",
    title: t("扫描", "Scanner"),
    params: { panelType: "scanner" },
    position: { referenceGroup: rightDock.group },
  });
  api.addPanel<RightDockParams>({
    id: "dock-drawings",
    component: "drawings",
    title: t("画线", "Drawings"),
    params: { panelType: "drawings" },
    position: { referenceGroup: rightDock.group },
  });
  api.addPanel<RightDockParams>({
    id: "dock-annotations",
    component: "annotations",
    title: t("标注", "Annotations"),
    params: { panelType: "annotations" },
    position: { referenceGroup: rightDock.group },
  });

  // Focus watchlist & primary chart cleanly.
  rightDock.api.setActive();
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
      return parsed;
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
