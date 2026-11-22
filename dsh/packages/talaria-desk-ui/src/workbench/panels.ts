export const PANEL_LABELS = {
  chart: "图表",
  alerts: "预警",
  scanner: "扫描",
  levels: "关键位",
  accounts: "账户",
} as const;

export type PanelName = keyof typeof PANEL_LABELS;
export const PANELS = Object.keys(PANEL_LABELS) as PanelName[];
export const DESK_PATH = "/desk";
export const LINK_PATH = "/desk/link";

export function panelFromPath(path: string): PanelName | undefined {
  return PANELS.find((panel) => path.replace(/\/$/, "") === panelPath(panel));
}

export function panelPath(panel: PanelName): string {
  return `${DESK_PATH}/${panel}`;
}

export const DEFAULT_INSTRUMENT = "BTC-USDT-PERP";
export const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"] as const;
