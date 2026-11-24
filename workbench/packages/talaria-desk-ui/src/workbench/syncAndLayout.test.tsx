import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { VELA_ATTRIBUTION, type DrawingSyncSource } from "../chart/adapter";
import { webglBudgetManager } from "../chart/webglBudget";
import { isMobileDrawerViewport, isReadOnlyViewport } from "../features/drawings/responsive";
import { api } from "../lib/api";
import { createLink, type LinkMessage, type LinkTransport } from "./link";
import { WorkbenchShell } from "./WorkbenchShell";

function createMemoryBus() {
  const listeners = new Set<(msg: LinkMessage) => void>();
  const makeTransport = (): LinkTransport => ({
    send: (msg) => {
      for (const listener of listeners) listener(msg);
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  });
  return { makeTransport };
}

describe("BroadcastChannel sync, responsive layout, and Vela attribution", () => {
  it("synchronizes symbol, timeframe, and crosshair across linked panes and popouts", async () => {
    const bus = createMemoryBus();
    const mainPane = createLink({
      key: "chart-1m",
      panel: "chart",
      initialTimeframe: "1m",
      syncTimeframe: false,
      transport: bus.makeTransport(),
    });
    const popoutPane = createLink({
      key: "popout-1",
      panel: "chart",
      initialTimeframe: "5m",
      syncTimeframe: true,
      transport: bus.makeTransport(),
    });

    mainPane.start();
    popoutPane.start();

    mainPane.setSymbol("ETH-USDT-PERP", "1m");
    expect(popoutPane.getSnapshot().instrumentId).toBe("ETH-USDT-PERP");
    // Multi-TF chart kept its own timeframe while popout synced symbol.
    expect(popoutPane.getSnapshot().timeframe).toBe("5m");

    popoutPane.setTimeframe("15m", true);
    expect(mainPane.getSnapshot().instrumentId).toBe("ETH-USDT-PERP");
    expect(mainPane.getSnapshot().timeframe).toBe("1m");

    mainPane.sendCrosshair(1_700_000_060_000, 3200.5);
    await new Promise<void>((resolve) =>
      requestAnimationFrame(() => resolve()),
    );
    expect(popoutPane.getCrosshair()).toEqual({
      ts: 1_700_000_060_000,
      price: 3200.5,
    });

    mainPane.stop();
    popoutPane.stop();
  });

  it("enforces responsive drawer and read-only states for 390x844 mobile viewport", () => {
    expect(isMobileDrawerViewport(390)).toBe(true);
    expect(isReadOnlyViewport(390)).toBe(true);
    expect(isMobileDrawerViewport(1440)).toBe(false);
    expect(isReadOnlyViewport(1440)).toBe(false);
  });

  it("renders visible Vela attribution and WebGL2 > 8 degradation banner", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({}),
      }),
    );

    // Simulate > 8 active charts in WebglBudgetManager
    webglBudgetManager.reset();
    for (let i = 1; i <= 9; i += 1) {
      webglBudgetManager.registerChart(`test-chart-${i}`);
    }

    // Force narrow 390px viewport so Dockview does not require real WebGL/canvas layout
    Object.defineProperty(window, "innerWidth", { value: 390, configurable: true });

    render(
      <WorkbenchShell
        initialTheme="dark"
        onThemeChange={() => undefined}
        popoutConfig={{
          panel: "chart",
          timeframe: "1m",
          instrumentId: "BTC-USDT-PERP",
          paneId: "test-popout",
        }}
      />,
    );

    const attr = screen.getByTestId("vela-attribution");
    expect(attr.textContent).toContain(VELA_ATTRIBUTION.label);
    const linkEl = attr.querySelector("a");
    expect(linkEl?.getAttribute("href")).toBe(VELA_ATTRIBUTION.url);

    const budgetNotice = screen.getByTestId("webgl-budget-notice");
    expect(budgetNotice.textContent).toContain("Canvas2D");

    webglBudgetManager.reset();
    Object.defineProperty(window, "innerWidth", { value: 1440, configurable: true });
    vi.unstubAllGlobals();
  });

  it("stops multi-pane drawing sync after one peer load and formats ?live=true", async () => {
    const bus = createMemoryBus();
    const pane1m = createLink({
      key: "chart-1m",
      panel: "chart",
      initialTimeframe: "1m",
      syncTimeframe: false,
      transport: bus.makeTransport(),
    });
    const pane5m = createLink({
      key: "chart-5m",
      panel: "chart",
      initialTimeframe: "5m",
      syncTimeframe: false,
      transport: bus.makeTransport(),
    });
    pane1m.start();
    pane5m.start();

    let peerFetches = 0;
    const emitFrom = (link: ReturnType<typeof createLink>, source: DrawingSyncSource) => {
      if (source === "mutation") {
        link.notifyDrawingsChanged("BTC-USDT-PERP");
      }
    };

    const unsub1m = pane1m.subscribeDrawings("BTC-USDT-PERP", () => {
      peerFetches += 1;
      emitFrom(pane1m, "load");
    });
    const unsub5m = pane5m.subscribeDrawings("BTC-USDT-PERP", () => {
      peerFetches += 1;
      emitFrom(pane5m, "load");
    });

    // Simulate initial load on both panes + one mutation on pane1m.
    emitFrom(pane1m, "load");
    emitFrom(pane5m, "load");
    expect(peerFetches).toBe(0);

    emitFrom(pane1m, "mutation");
    expect(peerFetches).toBe(1);

    unsub1m();
    unsub5m();
    pane1m.stop();
    pane5m.stop();

    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ snapshot_id: "s1", instrument_id: "BTC-USDT-PERP", timeframe: "1m", series: [] }),
    });
    vi.stubGlobal("fetch", fetchSpy);

    await api.indicators("snap-1", "1m", ["EMA"], 300, true);
    await api.computeOperator(
      { snapshot_id: "snap-1", timeframe: "1m", operator: "EMA", params: { period: 9 } },
      true,
    );

    const urls = fetchSpy.mock.calls.map((c) => String(c[0]));
    expect(urls[0]).toContain("live=true");
    expect(urls[0]).not.toContain("live=1");
    expect(urls[1]).toContain("live=true");
    expect(urls[1]).not.toContain("live=1");
    vi.unstubAllGlobals();
  });
});
