import { fireEvent, render, screen } from "@testing-library/react";
import type { DockviewApi } from "dockview-react";
import { describe, expect, it, vi } from "vitest";

import { AlertPanel } from "../features/alerts/AlertPanel";
import type { AlertsController } from "../features/alerts/useAlerts";
import { DeskSocketHub } from "../features/feed/deskSocketHub";
import {
  RIGHT_DOCK_TABS,
  ensureAllRightDockPanels,
  ensureRightDockPanel,
  findRightDockGroup,
  syncRightDockTitles,
} from "./layoutStore";

interface MockPanel {
  id: string;
  title: string;
  group: MockGroup;
  api: {
    id: string;
    setTitle: (next: string) => void;
    setActive: () => void;
  };
}

interface MockGroup {
  id: string;
  panels: MockPanel[];
  api: {
    isVisible: boolean;
    setVisible: (v: boolean) => void;
  };
}

function createMockDockApi() {
  const groups: MockGroup[] = [];
  const panelMap = new Map<string, MockPanel>();
  let groupSeq = 1;

  const makeGroup = (): MockGroup => {
    const group: MockGroup = {
      id: `group-${groupSeq++}`,
      panels: [],
      api: {
        isVisible: true,
        setVisible(v: boolean) {
          group.api.isVisible = v;
        },
      },
    };
    groups.push(group);
    return group;
  };

  const chartGroup = makeGroup();
  const chartPanel: MockPanel = {
    id: "chart-1m",
    title: "BTC-USDT-PERP · 1m",
    group: chartGroup,
    api: {
      id: "chart-1m",
      setTitle(next: string) {
        chartPanel.title = next;
      },
      setActive() {},
    },
  };
  chartGroup.panels.push(chartPanel);
  panelMap.set(chartPanel.id, chartPanel);

  const dockApi = {
    groups,
    panels: [] as MockPanel[],
    getPanel(id: string) {
      return panelMap.get(id);
    },
    addPanel(opts: {
      id: string;
      title?: string;
      position?: { referenceGroup?: MockGroup; direction?: string };
    }) {
      const targetGroup = opts.position?.referenceGroup ?? makeGroup();
      const panel: MockPanel = {
        id: opts.id,
        title: opts.title ?? opts.id,
        group: targetGroup,
        api: {
          id: opts.id,
          setTitle(next: string) {
            panel.title = next;
          },
          setActive() {},
        },
      };
      targetGroup.panels.push(panel);
      panelMap.set(panel.id, panel);
      return panel;
    },
    removePanel(panel: MockPanel) {
      panelMap.delete(panel.id);
      const idx = panel.group.panels.indexOf(panel);
      if (idx >= 0) panel.group.panels.splice(idx, 1);
      if (panel.group.panels.length === 0) {
        const gIdx = groups.indexOf(panel.group);
        if (gIdx >= 0) groups.splice(gIdx, 1);
      }
    },
  };

  return dockApi as unknown as DockviewApi & {
    removePanel: (p: unknown) => void;
    groups: MockGroup[];
  };
}

function makeAlertsController(): AlertsController {
  return {
    alerts: [],
    triggers: [],
    toast: null,
    unread: 0,
    loading: false,
    error: null,
    soundEnabled: false,
    setSoundEnabled: vi.fn(),
    refresh: vi.fn(async () => undefined),
    create: vi.fn(async () => null),
    update: vi.fn(async () => null),
    remove: vi.fn(async () => undefined),
    confirm: vi.fn(async () => undefined),
    dismiss: vi.fn(async () => undefined),
    ingestTrigger: vi.fn(),
    markSeen: vi.fn(),
    clearToast: vi.fn(),
  };
}

describe("Right-dock layout helpers, AlertPanel seed consumption, and global WS events", () => {
  it("locates right-dock group after closing watchlist, recreates closed tabs, and syncs locale titles", () => {
    const dock = createMockDockApi();
    expect(findRightDockGroup(dock)).toBeUndefined();

    const first = ensureAllRightDockPanels(dock, "zh");
    expect(first.id).toBe("dock-watchlist");
    const group = findRightDockGroup(dock);
    expect(group).toBeDefined();
    expect(group?.panels.map((p) => p.id)).toEqual(
      RIGHT_DOCK_TABS.map((tab) => `dock-${tab}`),
    );
    expect(dock.getPanel("dock-levels")?.api).toBeDefined();
    expect((dock.getPanel("dock-levels") as unknown as MockPanel).title).toBe("结构位");

    // Closing dock-watchlist leaves the remaining 8 tabs in the right group.
    dock.removePanel(first);
    expect(dock.getPanel("dock-watchlist")).toBeUndefined();
    const afterCloseOne = findRightDockGroup(dock);
    expect(afterCloseOne).toBe(group);
    expect(afterCloseOne?.panels.length).toBe(8);

    // Syncing locale updates all open right-dock tabs without touching chart panels.
    syncRightDockTitles(dock, "en");
    expect((dock.getPanel("dock-levels") as unknown as MockPanel).title).toBe("Structures");
    expect((dock.getPanel("dock-account") as unknown as MockPanel).title).toBe("Paper");
    expect((dock.getPanel("chart-1m") as unknown as MockPanel).title).toBe("BTC-USDT-PERP · 1m");

    syncRightDockTitles(dock, "zh");
    expect((dock.getPanel("dock-levels") as unknown as MockPanel).title).toBe("结构位");

    // ensureRightDockPanel restores a single closed tab back into the same group.
    const restoredWatchlist = ensureRightDockPanel(dock, "watchlist", "zh");
    expect(restoredWatchlist.group).toBe(group);

    // Closing every right-dock panel leaves findRightDockGroup undefined, and
    // ensureAllRightDockPanels cleanly recreates all 9 panels.
    for (const tab of RIGHT_DOCK_TABS) {
      const p = dock.getPanel(`dock-${tab}`);
      if (p) dock.removePanel(p);
    }
    expect(findRightDockGroup(dock)).toBeUndefined();

    ensureAllRightDockPanels(dock, "en");
    const recreated = findRightDockGroup(dock);
    expect(recreated?.panels.length).toBe(RIGHT_DOCK_TABS.length);
    expect((dock.getPanel("dock-watchlist") as unknown as MockPanel).title).toBe("Watchlist");
  });

  it("consumes formSeed once and does not reopen the form on subsequent prop updates", () => {
    const controller = makeAlertsController();
    const onSeedConsumed = vi.fn();
    const seed = {
      nonce: 101,
      price: 68500,
      instrumentId: "BTC-USDT-PERP",
      timeframe: "1m",
    };

    const { rerender } = render(
      <AlertPanel
        controller={controller}
        instruments={[]}
        drawings={[]}
        currentInstrument="BTC-USDT-PERP"
        currentTimeframe="1m"
        formSeed={seed}
        onSeedConsumed={onSeedConsumed}
        onLocate={vi.fn()}
      />,
    );

    expect(onSeedConsumed).toHaveBeenCalledTimes(1);
    expect(onSeedConsumed).toHaveBeenCalledWith(101);
    expect(screen.getByTestId("alert-create-open").textContent).toMatch(/取消|Cancel/);

    // User cancels the form.
    fireEvent.click(screen.getByTestId("alert-create-open"));
    expect(screen.getByTestId("alert-create-open").textContent).toMatch(/新建预警|New alert/);

    // Rerendering with cleared seed or symbol/timeframe switch must NOT reopen the form.
    rerender(
      <AlertPanel
        controller={controller}
        instruments={[]}
        drawings={[]}
        currentInstrument="ETH-USDT-PERP"
        currentTimeframe="5m"
        formSeed={seed}
        onSeedConsumed={onSeedConsumed}
        onLocate={vi.fn()}
      />,
    );
    expect(onSeedConsumed).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("alert-create-open").textContent).toMatch(/新建预警|New alert/);

    rerender(
      <AlertPanel
        controller={controller}
        instruments={[]}
        drawings={[]}
        currentInstrument="ETH-USDT-PERP"
        currentTimeframe="5m"
        formSeed={null}
        onSeedConsumed={onSeedConsumed}
        onLocate={vi.fn()}
      />,
    );
    expect(screen.getByTestId("alert-create-open").textContent).toMatch(/新建预警|New alert/);
  });

  it("delivers alert, alert_quota, and scanner events to global listeners without chart subscriptions", () => {
    class MockWebSocket {
      static OPEN = 1;
      readyState = MockWebSocket.OPEN;
      onopen: (() => void) | null = null;
      onmessage: ((ev: { data: string }) => void) | null = null;
      onerror: (() => void) | null = null;
      onclose: (() => void) | null = null;
      send = vi.fn();
      close = vi.fn();
    }

    const sockets: MockWebSocket[] = [];
    vi.stubGlobal(
      "WebSocket",
      class extends MockWebSocket {
        constructor() {
          super();
          sockets.push(this);
        }
      },
    );

    const hub = new DeskSocketHub();
    const onAlert = vi.fn();
    const onAlertQuota = vi.fn();
    const onScanner = vi.fn();

    const unsub = hub.subscribeGlobalEvents({ onAlert, onAlertQuota, onScanner });
    expect(sockets.length).toBe(1);

    const sock = sockets[0];
    sock.onopen?.();
    sock.onmessage?.({
      data: JSON.stringify({
        type: "hello",
        feed_mode: "replay",
        server_ts: 1_700_000_000_000,
        live: false,
        denied_alerts: ["a-denied-1"],
      }),
    });
    expect(onAlertQuota).toHaveBeenCalledWith(["a-denied-1"]);

    sock.onmessage?.({
      data: JSON.stringify({
        type: "alert",
        data: {
          trigger_id: "t-1",
          alert_id: "a-1",
          instrument_id: "BTC-USDT-PERP",
          triggered_ts: 1_700_000_060_000,
        },
      }),
    });
    expect(onAlert).toHaveBeenCalledTimes(1);

    sock.onmessage?.({
      data: JSON.stringify({
        type: "scanner",
        data: {
          trigger_id: "s-1",
          instrument_id: "BTC-USDT-PERP",
          kind: "VOLUME_SPIKE",
          created_ts: 1_700_000_060_000,
          snapshot_id: "snap-1",
        },
      }),
    });
    expect(onScanner).toHaveBeenCalledTimes(1);

    unsub();
    expect(sock.close).toHaveBeenCalledTimes(1);
    hub.destroy();
    vi.unstubAllGlobals();
  });
});
