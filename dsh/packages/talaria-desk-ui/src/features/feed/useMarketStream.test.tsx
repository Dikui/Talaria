import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, expect, it, vi } from "vitest";
import { useMarketStream } from "./useMarketStream";
import { parseDeskMessage } from "./messages";
import { feedReducer, initialFeedState } from "./reducer";

// data-access/06. Execute the hook's mount effects with a deterministic WS and clock.
const effects = vi.hoisted(() => [] as (() => void | (() => void))[]);
vi.mock("react", async (importOriginal) => ({
  ...await importOriginal<typeof import("react")>(),
  useEffect: (effect: () => void) => effects.push(effect),
}));

afterEach(() => {
  effects.length = 0;
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it("retries a denied chart until the channel is available", () => {
  vi.useFakeTimers();
  const send = vi.fn();
  let socket: Socket;
  class Socket {
    static OPEN = 1;
    readyState = 1;
    onopen = () => {};
    onmessage = (_: { data: string }) => {};
    send = send;
    close = () => {};
    constructor() { socket = this; }
  }
  vi.stubGlobal("WebSocket", Socket);
  vi.stubGlobal("window", { location: { host: "localhost", protocol: "http:" }, setTimeout, clearTimeout });
  function Chart() { useMarketStream("BTC", "1m"); return null; }
  renderToStaticMarkup(createElement(Chart));
  const cleanup = effects.map(effect => effect());
  socket!.onopen();
  const subscribe = JSON.stringify({ op: "subscribe", instrument_id: "BTC", timeframe: "1m" });
  socket!.onmessage({ data: JSON.stringify({
    type: "error", code: "SUBSCRIPTION_QUOTA", owner: "chart", instrument_id: "BTC", timeframe: "1m", message: "配额不足",
  }) });
  const before = send.mock.calls.length;
  vi.advanceTimersByTime(10_000);
  expect(send.mock.calls.slice(before)).toEqual([[subscribe]]);
  cleanup.forEach(stop => stop?.());
});

it("restores alert quota status on reconnect and clears it on recovery", () => {
  const apply = (state: typeof initialFeedState, payload: object) => feedReducer(state, {
    type: "message", message: parseDeskMessage(JSON.stringify(payload))!,
  });
  const denied = apply(initialFeedState, { type: "hello", denied_alerts: ["alert-a"] });
  expect(denied.deniedAlerts).toEqual(["alert-a"]);
  expect(apply(denied, { type: "alert_quota", denied_alerts: [] }).deniedAlerts).toEqual([]);
});
