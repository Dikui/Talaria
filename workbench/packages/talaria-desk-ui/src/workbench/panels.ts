import { getLocale, type Locale } from "../lib/i18n";

export const PANEL_LABELS = {
  chart: "图表",
  watchlist: "自选",
  alerts: "预警",
  scanner: "扫描",
  levels: "结构位",
  drawings: "画线",
  annotations: "标注",
  runs: "回测",
  drafts: "草案",
  account: "模拟盘",
} as const;

export type PanelName = keyof typeof PANEL_LABELS;

const ENGLISH_LABELS: Record<PanelName, string> = {
  chart: "Chart",
  watchlist: "Watchlist",
  alerts: "Alerts",
  scanner: "Scanner",
  levels: "Structures",
  drawings: "Drawings",
  annotations: "Annotations",
  runs: "Runs",
  drafts: "Drafts",
  account: "Paper",
};

export function panelLabel(panel: PanelName, locale: Locale = getLocale()): string {
  return (locale === "zh" ? PANEL_LABELS : ENGLISH_LABELS)[panel];
}

export const PANELS = Object.keys(PANEL_LABELS) as PanelName[];
export const DESK_PATH = "/desk";
export const LINK_PATH = "/desk/link";

export function panelFromPath(path: string): PanelName | undefined {
  const normalized = path.replace(/\/$/, "");
  return PANELS.find((panel) => normalized === panelPath(panel));
}

export function panelPath(panel: PanelName): string {
  return `${DESK_PATH}/${panel}`;
}

export const DEFAULT_INSTRUMENT = "BTC-USDT-PERP";
export const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"] as const;
