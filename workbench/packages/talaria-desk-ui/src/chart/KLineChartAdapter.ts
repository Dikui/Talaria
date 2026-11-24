import type { DrawingsDocument } from "@luxalgo/vela";
import type {
  AgentAnnotationRecord,
  CompactBar,
  DrawingRecord,
  IndicatorSeries,
  KeyLevel,
  KlineEvent,
} from "@talaria/contracts";

import type { Locale } from "../lib/i18n";
import type { Theme } from "../lib/theme";
import type {
  ChartMountOptions,
  ChartSurfaceAdapter,
  ExecutionBarrierItem,
  RenderBackendMode,
} from "./adapter";

export interface KLineChartFallbackInstance {
  dispose?(): void;
  resize?(): void;
}

export interface KLineChartRuntime {
  init(container: HTMLElement): KLineChartFallbackInstance;
}

/**
 * Optional fallback contract implementing `ChartSurfaceAdapter` for KLineChart v10.
 * Kept free of any `klinecharts` package dependency so the production workbench bundle
 * stays strictly on `@luxalgo/vela` (Apache-2.0) unless an explicit runtime is injected.
 */
export class KLineChartAdapter implements ChartSurfaceAdapter {
  private instance: KLineChartFallbackInstance | null = null;
  private readonly runtime: KLineChartRuntime | null;

  constructor(runtime: KLineChartRuntime | null = null) {
    this.runtime = runtime;
  }

  mount(container: HTMLElement, _options: ChartMountOptions): void {
    if (!this.runtime) {
      throw new Error(
        "KLineChartAdapter fallback requires an explicit KLineChartRuntime; VelaChartAdapter is the default.",
      );
    }
    this.instance = this.runtime.init(container);
  }

  async setMarket(): Promise<void> {}
  setBars(_bars: CompactBar[]): void {}
  updateLiveBar(_event: KlineEvent): void {}
  setIndicators(_series: IndicatorSeries[]): void {}
  setSystemLevels(_levels: KeyLevel[]): void {}
  setAgentAnnotations(_annotations: AgentAnnotationRecord[]): void {}
  setExecutionBarriers(_barriers: ExecutionBarrierItem[]): void {}
  loadHumanDrawings(_drawings: DrawingRecord[]): void {}
  setDrawingTool(_tool: string | null): void {}
  selectHumanDrawing(_drawingId: string | null): void {}
  exportDrawingsDocument(): DrawingsDocument | null {
    return null;
  }
  setTheme(_theme: Theme): void {}
  setLocale(_locale: Locale): void {}
  setReadOnly(_readOnly: boolean): void {}
  setBackendMode(_backend: RenderBackendMode): void {}
  setExternalCrosshair(_time: number | null, _price?: number | null): void {}
  setVisibleRange(_range: { from: number; to: number }): void {}
  getVisibleRange(): { from: number; to: number } | null {
    return null;
  }
  async refresh(): Promise<void> {}
  resize(): void {
    this.instance?.resize?.();
  }
  destroy(): void {
    this.instance?.dispose?.();
    this.instance = null;
  }
}
