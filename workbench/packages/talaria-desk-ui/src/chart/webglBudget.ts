import { useSyncExternalStore } from "react";
import type { RenderBackendMode } from "./adapter";

export const MAX_WEBGL_CELLS = 8;

export interface WebglBudgetSnapshot {
  maxWebglCells: number;
  activeChartCount: number;
  webglCount: number;
  canvas2dFallbackCount: number;
  exceeded: boolean;
  degradedChartIds: string[];
}

/**
 * Tracks active chart surfaces across the workbench.
 * Vela defaults `maxWebglCells = 8`; when active charts exceed 8, excess panes
 * cleanly degrade to `canvas2d` and notify the user.
 */
export class WebglBudgetManager {
  readonly maxWebglCells: number;
  private readonly order: string[] = [];
  private readonly listeners = new Set<() => void>();
  private cachedSnapshot: WebglBudgetSnapshot;

  constructor(maxWebglCells = MAX_WEBGL_CELLS) {
    this.maxWebglCells = maxWebglCells;
    this.cachedSnapshot = this.computeSnapshot();
  }

  private computeSnapshot(): WebglBudgetSnapshot {
    const webglCount = Math.min(this.order.length, this.maxWebglCells);
    const degradedChartIds = this.order.slice(this.maxWebglCells);
    return {
      maxWebglCells: this.maxWebglCells,
      activeChartCount: this.order.length,
      webglCount,
      canvas2dFallbackCount: degradedChartIds.length,
      exceeded: degradedChartIds.length > 0,
      degradedChartIds,
    };
  }

  private notify(): void {
    this.cachedSnapshot = this.computeSnapshot();
    for (const listener of this.listeners) {
      listener();
    }
  }

  registerChart(chartId: string): RenderBackendMode {
    if (!this.order.includes(chartId)) {
      this.order.push(chartId);
      this.notify();
    }
    return this.getBackendForChart(chartId);
  }

  unregisterChart(chartId: string): void {
    const idx = this.order.indexOf(chartId);
    if (idx >= 0) {
      this.order.splice(idx, 1);
      this.notify();
    }
  }

  getBackendForChart(chartId: string): RenderBackendMode {
    const idx = this.order.indexOf(chartId);
    if (idx < 0) {
      return this.order.length >= this.maxWebglCells ? "canvas2d" : "auto";
    }
    return idx >= this.maxWebglCells ? "canvas2d" : "auto";
  }

  snapshot = (): WebglBudgetSnapshot => {
    return this.cachedSnapshot;
  };

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  reset(): void {
    this.order.length = 0;
    this.notify();
  }
}

export const webglBudgetManager = new WebglBudgetManager(MAX_WEBGL_CELLS);

export function useWebglBudget(): WebglBudgetSnapshot {
  return useSyncExternalStore(
    webglBudgetManager.subscribe,
    webglBudgetManager.snapshot,
    webglBudgetManager.snapshot,
  );
}
