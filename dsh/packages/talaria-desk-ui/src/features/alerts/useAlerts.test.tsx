import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { AlertTriggerRecord } from "@talaria/contracts";
import { expect, it, vi } from "vitest";
import { api } from "../../lib/api";
import { useAlerts, type AlertsController } from "./useAlerts";

// 平台 02 审查：同一个 WebSocket 触发可被同页的多个面板接收。
it("starts one toast and one chime per trigger id across panels on the page", async () => {
  const play = vi.fn();
  const setTimeout = vi.fn();
  const ramp = { setValueAtTime() {}, exponentialRampToValueAtTime() {} };
  const controllers: AlertsController[] = [];
  // 浏览器声音和计时器替身用于观察响铃和 toast 生命周期。
  vi.stubGlobal("window", {
    localStorage: { getItem: () => "on" },
    setTimeout,
    clearTimeout: vi.fn(),
    AudioContext: class {
      createOscillator() {
        return { frequency: ramp, connect() {}, start: play, stop() {}, addEventListener() {} };
      }
      createGain() { return { gain: ramp, connect() {} }; }
    },
  });
  vi.spyOn(api, "alerts").mockResolvedValue({ alerts: [] });
  vi.spyOn(api, "alertTriggers").mockResolvedValue({ triggers: [] });
  function Panel() {
    controllers.push(useAlerts());
    return null;
  }
  const event: AlertTriggerRecord = {
    trigger_id: "trigger-a",
    alert_id: "alert-a",
    instrument_id: "BTC-USDT-PERP",
    timeframe: "1m",
    condition_kind: "METRIC_THRESHOLD",
    triggered_ts: 60_000,
    snapshot_id: "snapshot-a",
    message: "Price above threshold",
  };
  try {
    renderToStaticMarkup(createElement(Panel));
    renderToStaticMarkup(createElement(Panel));
    controllers[0].ingestTrigger(event);
    controllers[1].ingestTrigger({ ...event });
    expect(setTimeout).toHaveBeenCalledTimes(1);
    expect(play).toHaveBeenCalledTimes(1);

    controllers[1].ingestTrigger({ ...event, trigger_id: "trigger-b" });
    expect(setTimeout).toHaveBeenCalledTimes(2);
    expect(play).toHaveBeenCalledTimes(2);
    await Promise.resolve();
  } finally {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  }
});
