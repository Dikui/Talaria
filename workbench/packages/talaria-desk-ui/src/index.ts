import type { CompactBar, IndicatorSeries, KeyLevel } from "@talaria/contracts";

/**
 * Narrow surface contract isolating the charting library (`VelaChartAdapter` primary,
 * `KLineChartAdapter` optional fallback) from React dockview workspace components.
 */
export interface ChartSurfaceAdapter {
  mount(container: HTMLElement): void;
  setBars(bars: CompactBar[]): void;
  setIndicators(series: IndicatorSeries[]): void;
  setSystemLevels(levels: KeyLevel[]): void;
  destroy(): void;
}

export const VELA_ATTRIBUTION = {
  label: "Vela",
  url: "https://velacharts.dev",
} as const;
