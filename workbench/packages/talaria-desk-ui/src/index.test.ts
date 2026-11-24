import { describe, expect, it } from "vitest";
import type { CompactBar } from "@talaria/contracts";
import { KLineChartAdapter, VELA_ATTRIBUTION } from "./index";

describe("talaria-desk-ui exports and attribution", () => {
  it("exports Vela attribution metadata and contract types", () => {
    const sample: CompactBar = {
      timestamp: 1_700_000_000_000,
      open: "100.0",
      high: "101.0",
      low: "99.5",
      close: "100.5",
      volume: "10.0",
      turnover: "1005.0",
      status: "NORMAL",
      ts_available: 1_700_000_060_000,
      precision: "SECOND_VERIFIED",
    };
    expect(sample.status).toBe("NORMAL");
    expect(VELA_ATTRIBUTION.label).toBe("Vela");
    expect(VELA_ATTRIBUTION.url).toBe("https://velacharts.dev");
  });

  it("provides KLineChartAdapter fallback contract without bundling klinecharts", () => {
    const adapter = new KLineChartAdapter();
    expect(() =>
      adapter.mount(document.createElement("div"), {
        chartId: "fallback-1",
        instrumentId: "BTC-USDT-PERP",
        timeframe: "1m",
        pricePrecision: 1,
        volumePrecision: 3,
        theme: "dark",
        locale: "en",
      }),
    ).toThrow(/KLineChartRuntime/);
  });
});
