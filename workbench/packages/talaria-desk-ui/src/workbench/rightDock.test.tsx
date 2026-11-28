import { fireEvent, render, screen } from "@testing-library/react";
import type { DockviewApi } from "dockview-react";
import { describe, expect, it, vi } from "vitest";

import { AlertPanel } from "../features/alerts/AlertPanel";
import type { AlertsController } from "../features/alerts/useAlerts";
import type { AnnotationsController } from "../features/annotations/useAnnotations";
import { DeskSocketHub } from "../features/feed/deskSocketHub";
import type { ScannerController } from "../features/scanner/useScanner";
import { defaultStructureFilter } from "../features/structures/model";
import {
  DEFAULT_SHELL_UI_STATE,
  applyPresetLayout,
  loadShellUiState,
  resolvePanelNavigation,
  saveShellUiState,
  toggleRightDrawerState,
} from "./layoutStore";
import { LayersPanel } from "./LayersPanel";
import { MonitorPanel } from "./MonitorPanel";
import { TradePanel } from "./TradePanel";

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
      component?: string;
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
    clear() {
      groups.length = 0;
      panelMap.clear();
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

function makeScannerController(): ScannerController {
  return {
    triggers: [],
    focus: [],
    loading: false,
    scanning: false,
    error: null,
    unread: 0,
    refresh: vi.fn(async () => undefined),
    scanNow: vi.fn(async () => undefined),
    ingest: vi.fn(),
    markSeen: vi.fn(),
  };
}

function makeAnnotationsController(): AnnotationsController {
  return {
    annotations: [],
    loading: false,
    error: null,
    refresh: vi.fn(async () => undefined),
    adopt: vi.fn(async () => undefined),
    dismiss: vi.fn(async () => undefined),
  };
}

describe("Scheme B layout helpers, consolidated panels, AlertPanel seed consumption, and global WS events", () => {
  it("applies chart-only Dockview presets and manages Scheme B shell navigation & storage", () => {
    const dock = createMockDockApi();
    applyPresetLayout(dock, "multi-tf");
    expect(dock.getPanel("chart-1m")).toBeDefined();
    expect(dock.getPanel("chart-5m")).toBeDefined();
    expect(dock.getPanel("chart-1h")).toBeDefined();
    expect(dock.getPanel("dock-watchlist")).toBeUndefined();

    // Toggling the same rail icon collapses the drawer; clicking another switches to it.
    const collapsed = toggleRightDrawerState(DEFAULT_SHELL_UI_STATE, "watchlist");
    expect(collapsed.rightDrawer).toBeNull();
    const switched = toggleRightDrawerState(collapsed, "monitor");
    expect(switched.rightDrawer).toBe("monitor");

    // Legacy & consolidated panel targets map cleanly to Scheme B regions.
    const toAlerts = resolvePanelNavigation(DEFAULT_SHELL_UI_STATE, "alerts");
    expect(toAlerts.mode).toBe("live");
    expect(toAlerts.rightDrawer).toBe("monitor");
    expect(toAlerts.monitorTab).toBe("alerts");

    const toLevels = resolvePanelNavigation(DEFAULT_SHELL_UI_STATE, "levels");
    expect(toLevels.rightDrawer).toBe("layers");
    expect(toLevels.layerSource).toBe("structure");

    const toDrafts = resolvePanelNavigation(DEFAULT_SHELL_UI_STATE, "drafts");
    expect(toDrafts.mode).toBe("live");
    expect(toDrafts.tradeDrawerOpen).toBe(true);
    expect(toDrafts.tradeTab).toBe("drafts");

    const toRuns = resolvePanelNavigation(DEFAULT_SHELL_UI_STATE, "runs");
    expect(toRuns.mode).toBe("backtest");
    expect(toRuns.backtestDrawerOpen).toBe(true);

    // Persists & clamps shell UI state safely.
    const mem = new Map<string, string>();
    const storage = {
      getItem: (k: string) => mem.get(k) ?? null,
      setItem: (k: string, v: string) => {
        mem.set(k, v);
      },
    };
    saveShellUiState(
      {
        ...toRuns,
        rightDrawerWidth: 999,
        tradeDrawerHeight: 50,
      },
      storage,
    );
    const loaded = loadShellUiState(storage);
    expect(loaded.mode).toBe("backtest");
    expect(loaded.rightDrawerWidth).toBe(560);
    expect(loaded.tradeDrawerHeight).toBe(180);
  });

  it("switches consolidated MonitorPanel, LayersPanel, and TradePanel sub-views", () => {
    const onMonitorTab = vi.fn();
    const { rerender: rerenderMonitor } = render(
      <MonitorPanel
        activeTab="scanner"
        onTabChange={onMonitorTab}
        scannerController={makeScannerController()}
        alertsController={makeAlertsController()}
        deniedAlerts={[]}
        instrumentId="BTC-USDT-PERP"
        timeframe="1m"
        instruments={[]}
        drawings={[]}
        link={{ subscribeDrawings: () => () => undefined }}
        alertSeed={null}
        onSeedConsumed={vi.fn()}
        onLocate={vi.fn()}
        readOnly={false}
        replayActive={false}
      />,
    );
    expect(screen.getByTestId("scanner-dock")).toBeTruthy();
    fireEvent.click(screen.getByTestId("monitor-tab-alerts"));
    expect(onMonitorTab).toHaveBeenCalledWith("alerts");
    rerenderMonitor(
      <MonitorPanel
        activeTab="alerts"
        onTabChange={onMonitorTab}
        scannerController={makeScannerController()}
        alertsController={makeAlertsController()}
        deniedAlerts={[]}
        instrumentId="BTC-USDT-PERP"
        timeframe="1m"
        instruments={[]}
        drawings={[]}
        link={{ subscribeDrawings: () => () => undefined }}
        alertSeed={null}
        onSeedConsumed={vi.fn()}
        onLocate={vi.fn()}
        readOnly={false}
        replayActive={false}
      />,
    );
    expect(screen.getByTestId("alerts-panel")).toBeTruthy();

    const onLayerSource = vi.fn();
    render(
      <LayersPanel
        sourceFilter="all"
        onSourceChange={onLayerSource}
        structureProps={{
          structures: [],
          allStructures: [],
          activeTimeframe: "1m",
          filter: defaultStructureFilter,
          onFilterChange: vi.fn(),
          precision: 2,
          ignoredCount: 0,
          onIgnore: vi.fn(),
          ignoringId: null,
          onAdopt: vi.fn(),
          adoptingId: null,
          adoptedIds: new Set(),
          onAttachAlert: vi.fn(),
          loading: false,
          error: null,
          readOnly: false,
        }}
        drawingProps={{
          drawings: [],
          selectedId: null,
          readOnly: false,
          onSelect: vi.fn(),
          onDelete: vi.fn(),
          onAttachAlert: vi.fn(),
        }}
        annotationProps={{
          controller: makeAnnotationsController(),
          readOnly: false,
        }}
      />,
    );
    expect(screen.getByTestId("layers-all-stack")).toBeTruthy();
    fireEvent.click(screen.getByTestId("layers-source-drawing"));
    expect(onLayerSource).toHaveBeenCalledWith("drawing");

    const onTradeTab = vi.fn();
    const onCollapse = vi.fn();
    render(
      <TradePanel
        activeTab="account"
        onTabChange={onTradeTab}
        onCollapse={onCollapse}
        draftPanelProps={{
          activeInstrumentId: "BTC-USDT-PERP",
          activeTimeframe: "1m",
          pollIntervalMs: 0,
        }}
      />,
    );
    fireEvent.click(screen.getByTestId("trade-tab-drafts"));
    expect(onTradeTab).toHaveBeenCalledWith("drafts");
    fireEvent.click(screen.getByTestId("trade-drawer-collapse"));
    expect(onCollapse).toHaveBeenCalledTimes(1);
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
