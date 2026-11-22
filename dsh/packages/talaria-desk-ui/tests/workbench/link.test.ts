import { describe, expect, it, vi } from "vitest";
import { cacheRelaySelection, cachedRelayReply, createLink, type LinkMessage } from "../../src/workbench/link";

function workspace() {
  const listeners = new Set<(message: LinkMessage) => void>();
  const send = (message: LinkMessage) => listeners.forEach((listener) => listener(message));
  const storage = new Map<string, string>();
  const open = (key: string) => createLink({
    key,
    panel: "chart",
    transport: {
      send,
      subscribe(listener) {
        listeners.add(listener);
        return () => { listeners.delete(listener); };
      },
    },
    storage: {
      getItem: (name) => storage.get(name) ?? null,
      setItem: (name, value) => { storage.set(name, value); },
    },
  });
  return { open };
}

describe("平台 02 面板联动", () => {
  it("同组跟随标的、周期、十字光标和定位，其他组独立", () => {
    const { open } = workspace();
    const chart = open("main");
    const alerts = open("right");
    const other = open("other");
    other.setGroup("blue");
    alerts.setSymbol("ETH-USDT-PERP", "15m");
    alerts.sendCrosshair(42);
    expect(chart.getSnapshot()).toMatchObject({
      instrumentId: "ETH-USDT-PERP", timeframe: "15m", crosshair: 42,
    });
    alerts.locate({ instrument_id: "SOL-USDT-PERP", timeframe: "1h", triggered_ts: 123 });
    expect(chart.getSnapshot()).toMatchObject({
      instrumentId: "SOL-USDT-PERP", timeframe: "1h", locateTs: 123, crosshair: null,
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

it("restores the origin relay's last group selection after the sending window closes", () => {
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
  first.setSymbol("ETH-USDT-PERP", "1h");
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
  expect(next.getSnapshot()).toMatchObject({ instrumentId: "ETH-USDT-PERP", timeframe: "1h" });
  next.close();
});
