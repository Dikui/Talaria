import { describe, expect, it } from "vitest";
import { WebglBudgetManager } from "./webglBudget";

describe("WebglBudgetManager (>8 charts fallback)", () => {
  it("keeps first 8 charts on auto/WebGL2 and degrades chart 9+ cleanly to canvas2d", () => {
    const budget = new WebglBudgetManager(8);
    for (let i = 1; i <= 8; i += 1) {
      expect(budget.registerChart(`chart-${i}`)).toBe("auto");
    }
    expect(budget.snapshot().exceeded).toBe(false);
    expect(budget.snapshot().webglCount).toBe(8);
    expect(budget.snapshot().canvas2dFallbackCount).toBe(0);

    expect(budget.registerChart("chart-9")).toBe("canvas2d");
    expect(budget.registerChart("chart-10")).toBe("canvas2d");

    const snap = budget.snapshot();
    expect(snap.exceeded).toBe(true);
    expect(snap.activeChartCount).toBe(10);
    expect(snap.webglCount).toBe(8);
    expect(snap.canvas2dFallbackCount).toBe(2);
    expect(snap.degradedChartIds).toEqual(["chart-9", "chart-10"]);

    budget.unregisterChart("chart-1");
    budget.unregisterChart("chart-2");
    expect(budget.snapshot().exceeded).toBe(false);
    expect(budget.snapshot().activeChartCount).toBe(8);
  });
});
