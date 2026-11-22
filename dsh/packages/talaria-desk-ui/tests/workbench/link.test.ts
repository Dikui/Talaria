import { describe, expect, it, vi } from "vitest";
import { createLinkRelay } from "../../../talaria-dsh-plugin/client/linkRelay";
import { cacheRelaySelection, cachedRelayReply, createLink, type LinkMessage } from "../../src/workbench/link";
import { LINK_READY } from "../../src/workbench/transport";

// The plugin imports the desk package entry point, which also registers chart templates.
vi.mock("klinecharts", () => ({ registerIndicator: vi.fn() }));

function workspace() {
  const listeners = new Set<(message: LinkMessage) => void>();
  const send = (message: LinkMessage) => listeners.forEach((listener) => listener(message));
  const storage = new Map<string, string>();
  const open = (key: string, useStorage = true) => createLink({
    key,
    panel: "chart",
    transport: {
      send,
      subscribe(listener) {
        listeners.add(listener);
        return () => { listeners.delete(listener); };
      },
    },
    storage: useStorage ? {
      getItem: (name) => storage.get(name) ?? null,
      setItem: (name, value) => { storage.set(name, value); },
    } : undefined,
  });
  return { open };
}

describe("平台 02 面板联动", () => {
  it("同组跟随标的、周期和定位，其他组独立", () => {
    const { open } = workspace();
    const chart = open("main");
    const alerts = open("right");
    const other = open("other");
    other.setGroup("blue");
    alerts.setSymbol("ETH-USDT-PERP", "15m");
    expect(chart.getSnapshot()).toMatchObject({
      instrumentId: "ETH-USDT-PERP", timeframe: "15m",
    });
    alerts.locate({ instrument_id: "SOL-USDT-PERP", timeframe: "1h", triggered_ts: 123 });
    expect(chart.getSnapshot()).toMatchObject({
      instrumentId: "SOL-USDT-PERP", timeframe: "1h", locateTs: 123,
    });
    expect(other.getSnapshot()).toMatchObject({ instrumentId: "BTC-USDT-PERP", timeframe: "1m" });
    chart.setGroup("none");
    alerts.setSymbol("ETH-USDT-PERP", "5m");
    expect(chart.getSnapshot().instrumentId).toBe("SOL-USDT-PERP");
    chart.close();
    alerts.close();
    other.close();
  });

  it("加入和换组恢复该组标的，刷新保留面板的组", () => {
    const { open } = workspace();
    const first = open("main");
    first.setSymbol("ETH-USDT-PERP", "5m");
    const second = open("right");
    expect(second.getSnapshot().instrumentId).toBe("ETH-USDT-PERP");
    second.setGroup("green");
    second.setSymbol("SOL-USDT-PERP", "4h");
    second.setGroup("red");
    expect(second.getSnapshot().instrumentId).toBe("ETH-USDT-PERP");
    second.setGroup("green");
    second.close();

    const reopened = open("right");
    expect(reopened.getSnapshot()).toMatchObject({
      group: "green", instrumentId: "SOL-USDT-PERP", timeframe: "4h",
    });
    first.close();
    reopened.close();
  });

  it("窗口关闭后移出列表，失去心跳的窗口也会过期", () => {
    vi.useFakeTimers();
    try {
      const { open } = workspace();
      const chart = open("main");
      const other = open("right");
      expect(chart.getSnapshot().peers).toHaveLength(1);
      other.leave();
      expect(chart.getSnapshot().peers).toHaveLength(0);
      other.join();
      expect(chart.getSnapshot().peers).toHaveLength(1);
      other.close({ announce: false });
      vi.advanceTimersByTime(4_500);
      expect(chart.getSnapshot().peers).toHaveLength(0);
      chart.close();
    } finally {
      vi.useRealTimers();
    }
  });
});

it("gets the active selection from a peer with separate origin storage", () => {
  const listeners = new Set<(message: LinkMessage) => void>();
  const transport = {
    send: (message: LinkMessage) => listeners.forEach((listener) => listener(message)),
    subscribe(listener: (message: LinkMessage) => void) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
  };
  const chart = createLink({ key: "dsh", panel: "chart", transport });
  chart.setSymbol("1000BONK-USDT-PERP", "15m");
  const fallback = createLink({ key: "fallback", panel: "chart", transport });
  expect(fallback.getSnapshot()).toMatchObject({ instrumentId: "1000BONK-USDT-PERP", timeframe: "15m" });
  chart.close();
  fallback.close();
});

it("keeps unlinked selections private to separate tabs across refreshes", () => {
  const local = new Map<string, string>();
  const firstSession = new Map<string, string>();
  const secondSession = new Map<string, string>();
  const storage = (values: Map<string, string>) => ({
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
  });
  const open = (tab: Map<string, string>) => createLink({
    key: "same-panel", panel: "chart",
    storage: storage(local), preferences: storage(tab),
    transport: { subscribe: () => () => undefined, send: () => undefined },
  });
  const first = open(firstSession);
  const second = open(secondSession);
  first.setGroup("none");
  first.setSymbol("ETH-USDT-PERP", "5m");
  second.setGroup("none");
  second.setSymbol("SOL-USDT-PERP", "4h");
  first.setGroup("red");
  first.setGroup("none");
  expect(first.getSnapshot().instrumentId).toBe("ETH-USDT-PERP");
  first.close();
  second.close();

  const refreshed = open(firstSession);
  try {
    expect(refreshed.getSnapshot()).toMatchObject({ group: "none", instrumentId: "ETH-USDT-PERP", timeframe: "5m" });
  } finally {
    refreshed.close();
  }
});

it("restores the origin relay's last group locate target after the sending window closes", () => {
  const data = new Map<string, string>();
  const storage = {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => { data.set(key, value); },
  };
  const first = createLink({
    key: "dsh", panel: "chart",
    transport: {
      send: (message) => cacheRelaySelection(storage, message),
      subscribe: () => () => undefined,
    },
  });
  first.locate({ instrument_id: "ETH-USDT-PERP", timeframe: "1h", triggered_ts: 125 });
  first.close();

  let receive: (message: LinkMessage) => void;
  const next = createLink({
    key: "dsh-after-restart", panel: "chart",
    transport: {
      send(message) {
        const answer = cachedRelayReply(storage, message);
        if (answer) receive(answer);
      },
      subscribe(listener) {
        receive = listener;
        return () => undefined;
      },
    },
  });
  expect(next.getSnapshot()).toMatchObject({ instrumentId: "ETH-USDT-PERP", timeframe: "1h", locateTs: 125 });
  next.close();
});

// 平台 02 审查：两边分别保留断线期间的标的，重新加入后用较新的一次。
it.each(["returning", "present"] as const)("converges to the newer selection on rejoin (%s)", (newer) => {
  vi.useFakeTimers();
  const listeners = new Set<(message: LinkMessage) => void>();
  let connected = false;
  const transport = {
    send(message: LinkMessage) {
      if (connected) listeners.forEach((listener) => listener(message));
    },
    subscribe(listener: (message: LinkMessage) => void) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
  };
  const returning = createLink({ key: "returning", panel: "chart", transport });
  const present = createLink({ key: "present", panel: "chart", transport });
  try {
    vi.setSystemTime(newer === "returning" ? 200 : 100);
    returning.setSymbol("ETH-USDT-PERP", "5m");
    vi.setSystemTime(newer === "present" ? 200 : 100);
    present.setSymbol("SOL-USDT-PERP", "15m");
    connected = true;
    returning.join();
    const expected = newer === "returning"
      ? { instrumentId: "ETH-USDT-PERP", timeframe: "5m" }
      : { instrumentId: "SOL-USDT-PERP", timeframe: "15m" };
    expect(returning.getSnapshot()).toMatchObject(expected);
    expect(present.getSnapshot()).toMatchObject(expected);
  } finally {
    returning.close();
    present.close();
    vi.useRealTimers();
  }
});

it("resends changes made before the transport is ready", () => {
  let ready = false;
  let onReady: () => void = () => undefined;
  const received: LinkMessage[] = [];
  const link = createLink({
    key: "dsh", panel: "chart",
    transport: {
      send(message) { if (ready) received.push(message); },
      subscribe: () => () => undefined,
      onReady(listener) { onReady = listener; return () => undefined; },
    },
  });
  try {
    link.setSymbol("ETH-USDT-PERP", "5m");
    link.setSymbol("SOL-USDT-PERP", "15m");
    ready = true;
    onReady();
    expect(received).toContainEqual(expect.objectContaining({
      type: "ask", selection: expect.objectContaining({ instrumentId: "SOL-USDT-PERP", timeframe: "15m" }),
    }));
  } finally {
    link.close();
  }
});

it("flushes the newest buffered selection when the iframe connects", async () => {
  let servicePort: MessagePort | undefined;
  const frame = Object.assign(new EventTarget(), {
    sandbox: { add() {} },
    contentWindow: { postMessage(_type: string, _origin: string, [port]: MessagePort[]) { servicePort = port; } },
    remove() {},
  });
  vi.stubGlobal("document", { createElement: () => frame, body: { append() {} } });
  const relay = createLinkRelay("http://127.0.0.1:8765");
  try {
    relay.send({
      type: "symbol", group: "red", from: "first",
      selection: { instrumentId: "ETH-USDT-PERP", timeframe: "5m", revision: 200, author: "first" },
    });
    relay.send({
      type: "symbol", group: "red", from: "second",
      selection: { instrumentId: "SOL-USDT-PERP", timeframe: "15m", revision: 100, author: "second" },
    });
    frame.dispatchEvent(new Event("load"));
    const sent = new Promise<LinkMessage>((resolve) => {
      servicePort!.onmessage = ({ data }) => resolve(data);
    });
    servicePort!.postMessage(LINK_READY);
    await expect(sent).resolves.toMatchObject({
      type: "symbol", selection: { instrumentId: "ETH-USDT-PERP", timeframe: "5m" },
    });
  } finally {
    relay.close();
    servicePort?.close();
    vi.unstubAllGlobals();
  }
});

// 平台 02 审查：定位在 ask 回复、重开与中继缓存中都保留，直到手动切换。
it("locates charts joining from another origin and clears the target on manual selection", () => {
  const { open } = workspace();
  const alerts = open("alerts");
  alerts.locate({ instrument_id: "ETH-USDT-PERP", timeframe: "15m", triggered_ts: 123 });
  const chart = open("first-chart", false);
  try {
    expect(chart.getSnapshot()).toMatchObject({
      instrumentId: "ETH-USDT-PERP", timeframe: "15m", locateTs: 123,
    });
    alerts.close();
    chart.close();
    const reopened = open("second-chart");
    try {
      expect(reopened.getSnapshot().locateTs).toBe(123);
      reopened.setSymbol("ETH-USDT-PERP", "5m");
      const later = open("third-chart");
      try {
        expect(later.getSnapshot()).toMatchObject({ timeframe: "5m", locateTs: null });
      } finally {
        later.close();
      }
    } finally {
      reopened.close();
    }
  } finally {
    chart.close();
    alerts.close();
  }
});

// 平台 02 审查：快速移动只发一条最新时刻，接收方的面板快照不因光标更新。
it("sends at most one crosshair update per animation frame without publishing a panel snapshot", () => {
  vi.useFakeTimers();
  vi.stubGlobal("requestAnimationFrame", (callback: () => void) => setTimeout(callback, 16));
  vi.stubGlobal("cancelAnimationFrame", clearTimeout);
  const { open } = workspace();
  const source = open("source");
  const target = open("target");
  const panelChanged = vi.fn();
  target.subscribe(panelChanged);
  const crosshairChanged = vi.fn();
  target.subscribeCrosshair(crosshairChanged);
  const { getCrosshair } = target;
  try {
    source.sendCrosshair(10);
    source.sendCrosshair(20);
    source.sendCrosshair(30);
    expect(getCrosshair()).toBeNull();
    vi.advanceTimersByTime(16);
    expect(getCrosshair()).toBe(30);
    expect(crosshairChanged).toHaveBeenCalledTimes(1);
    expect(panelChanged).not.toHaveBeenCalled();

    source.sendCrosshair(40);
    source.sendCrosshair(null);
    vi.advanceTimersByTime(16);
    expect(getCrosshair()).toBeNull();
    expect(crosshairChanged).toHaveBeenCalledTimes(2);
    expect(panelChanged).not.toHaveBeenCalled();
  } finally {
    source.close();
    target.close();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  }
});

// 平台 02 审查：图表与预警面板按标的刷新画线，其他标的、组和来源面板不刷新。
it("invalidates drawing candidates and chart layers only for the instrument in the linked group", () => {
  const { open } = workspace();
  const source = open("source");
  const chart = open("chart");
  const alerts = open("alerts");
  const other = open("other");
  other.setGroup("blue");
  const chartRefresh = vi.fn();
  const alertRefresh = vi.fn();
  const ignoredRefresh = vi.fn();
  try {
    chart.subscribeDrawings("ETH-USDT-PERP", chartRefresh);
    alerts.subscribeDrawings("ETH-USDT-PERP", alertRefresh);
    alerts.subscribeDrawings("SOL-USDT-PERP", ignoredRefresh);
    other.subscribeDrawings("ETH-USDT-PERP", ignoredRefresh);
    source.subscribeDrawings("ETH-USDT-PERP", ignoredRefresh);
    source.notifyDrawingsChanged("ETH-USDT-PERP");
    expect(chartRefresh).toHaveBeenCalledTimes(1);
    expect(alertRefresh).toHaveBeenCalledTimes(1);
    expect(ignoredRefresh).not.toHaveBeenCalled();
  } finally {
    for (const link of [source, chart, alerts, other]) link.close();
  }
});
