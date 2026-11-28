import type { DockviewApi, SerializedDockview } from "dockview-react";

import { getLocale, type Locale } from "../lib/i18n";
import { panelLabel } from "./panels";

export const WORKBENCH_LAYOUT_STORAGE_KEY = "talaria-workbench-dockview-layout-v1";

export type LayoutPreset = "multi-tf" | "single" | "quad";

export interface ChartPaneParams {
  paneId: string;
  initialTimeframe: string;
  syncTimeframe: boolean;
}

export const RIGHT_DOCK_TABS = [
  "watchlist",
  "alerts",
  "levels",
  "scanner",
  "drawings",
  "annotations",
  "runs",
  "drafts",
  "account",
] as const;

export type RightDockTabId = (typeof RIGHT_DOCK_TABS)[number];

export interface RightDockParams {
  panelType: RightDockTabId;
}

type DockGroup = DockviewApi["groups"][number];
type DockPanel = NonNullable<ReturnType<DockviewApi["getPanel"]>>;

const RIGHT_DOCK_PANEL_IDS = new Set<string>(
  RIGHT_DOCK_TABS.map((tab) => `dock-${tab}`),
);

export function rightDockPanelId(tab: RightDockTabId): string {
  return `dock-${tab}`;
}

export function isRightDockPanelId(id: string): boolean {
  return RIGHT_DOCK_PANEL_IDS.has(id);
}

/**
 * Locate the Dockview group hosting the right-dock panels.
 * Prefers a pure right-dock group (highest panel count) and falls back to any
 * group containing right-dock panels so closing `自选` or splitting panels
 * never orphans the header toggle.
 */
export function findRightDockGroup(api: DockviewApi): DockGroup | undefined {
  let bestPure: { group: DockGroup; count: number } | null = null;
  let bestAny: { group: DockGroup; count: number } | null = null;
  for (const group of api.groups) {
    const rightCount = group.panels.filter((p) => isRightDockPanelId(p.id)).length;
    if (rightCount === 0) continue;
    const isPure = rightCount === group.panels.length;
    if (isPure && (!bestPure || rightCount > bestPure.count)) {
      bestPure = { group, count: rightCount };
    }
    if (!bestAny || rightCount > bestAny.count) {
      bestAny = { group, count: rightCount };
    }
  }
  return bestPure?.group ?? bestAny?.group;
}

/** Ensure a single right-dock tab exists in the right dock group and return it. */
export function ensureRightDockPanel(
  api: DockviewApi,
  tab: RightDockTabId,
  locale: Locale = getLocale(),
): DockPanel {
  const id = rightDockPanelId(tab);
  const existing = api.getPanel(id);
  if (existing) return existing;

  const group = findRightDockGroup(api);
  return api.addPanel<RightDockParams>({
    id,
    component: tab,
    title: panelLabel(tab, locale),
    params: { panelType: tab },
    ...(group
      ? { position: { referenceGroup: group } }
      : { position: { direction: "right" }, initialWidth: 340 }),
  });
}

/** Ensure all right-dock tabs exist in canonical order and return Watchlist. */
export function ensureAllRightDockPanels(
  api: DockviewApi,
  locale: Locale = getLocale(),
): DockPanel {
  let first: DockPanel | null = null;
  for (const tab of RIGHT_DOCK_TABS) {
    const panel = ensureRightDockPanel(api, tab, locale);
    if (!first) first = panel;
  }
  return first!;
}

/** Synchronize right-dock tab titles with the active UI locale. */
export function syncRightDockTitles(
  api: DockviewApi,
  locale: Locale = getLocale(),
): void {
  for (const tab of RIGHT_DOCK_TABS) {
    const panel = api.getPanel(rightDockPanelId(tab));
    if (panel) {
      panel.api.setTitle(panelLabel(tab, locale));
    }
  }
}

export function applyPresetLayout(
  api: DockviewApi,
  preset: LayoutPreset,
  locale: Locale = getLocale(),
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

  const rightDock = ensureAllRightDockPanels(api, locale);

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
