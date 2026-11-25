import type { IndicatorModel, LineLikeSeries, OHLCV } from "@luxalgo/vela";
import type { IndicatorSeries } from "@talaria/contracts";
import { afterEach, describe, expect, it, vi } from "vitest";

import { describeParams } from "../lib/indicatorData";
import {
  TALARIA_INDICATOR_LANGUAGE,
  clearAuthoritativeIndicatorSlot,
  createAuthoritativeIndicatorEngine,
  setAuthoritativeIndicatorLocale,
  setAuthoritativeIndicatorSlot,
} from "./velaIndicators";

function makeSeries(overrides: Partial<IndicatorSeries> = {}): IndicatorSeries {
  return {
    name: "OI_DIVERGENCE",
    pane: "sub",
    params: { period: 30 },
    points: [
      { timestamp: 1_000, values: { value: null } },
      { timestamp: 2_000, values: { value: "0.0123" } },
    ],
    summary: { latest: { value: "0.0123" }, warmup_bars: 1, valid_points: 1 },
    ...overrides,
  };
}

const BARS: OHLCV[] = [
  { time: 1_000, open: 100, high: 105, low: 95, close: 102 },
  { time: 2_000, open: 102, high: 108, low: 101, close: 107 },
];

describe("describeParams", () => {
  it("formats indicator titles with localized names and parameters", () => {
    expect(describeParams(makeSeries({ name: "EMA", pane: "main", params: { period: 9 } }), "zh")).toBe("EMA(9)");
    expect(describeParams(makeSeries({ name: "RSI", params: { period: 14 } }), "en")).toBe("RSI(14)");
    expect(
      describeParams(
        makeSeries({ name: "BOLL", pane: "main", params: { period: 20, std_dev: "2.50" } }),
        "zh",
      ),
    ).toBe("BOLL(20, 2.5σ)");
    expect(
      describeParams(
        makeSeries({ name: "VWAP", pane: "main", params: { period: 1, anchor: "session" } }),
        "zh",
      ),
    ).toBe("VWAP(UTC 日界)");
    expect(
      describeParams(
        makeSeries({ name: "VWAP", pane: "main", params: { period: 1, anchor: "snapshot" } }),
        "en",
      ),
    ).toBe("VWAP(snapshot start)");
    expect(describeParams(makeSeries({ name: "OI_DIVERGENCE", params: { period: 30 } }), "zh")).toBe(
      "OI 背离(30)",
    );
    expect(describeParams(makeSeries({ name: "OI_DIVERGENCE", params: { period: 30 } }), "en")).toBe(
      "OI divergence(30)",
    );
    expect(
      describeParams(makeSeries({ name: "FUNDING_QUANTILE", params: { period: 30 } }), "zh"),
    ).toBe("资金费率分位(30)");
    expect(
      describeParams(makeSeries({ name: "FUNDING_QUANTILE", params: { period: 30 } }), "en"),
    ).toBe("Funding rate quantile(30)");
    expect(describeParams(makeSeries({ name: "NET_ODDS", params: { period: 20 } }), "zh")).toBe(
      "净赔率(20)",
    );
    expect(describeParams(makeSeries({ name: "NET_ODDS", params: { period: 20 } }), "en")).toBe(
      "Net odds(20)",
    );
  });
});

describe("createAuthoritativeIndicatorEngine", () => {
  const touchedSlots: string[] = [];

  afterEach(() => {
    for (const slotKey of touchedSlots.splice(0)) {
      clearAuthoritativeIndicatorSlot(slotKey);
    }
  });

  it("prepares per-indicator metadata and emits localized title/shorttitle models", async () => {
    const oiSlot = "pane-1:OI_DIVERGENCE:sub";
    const emaSlot = "pane-1:EMA:main";
    touchedSlots.push(oiSlot, emaSlot);

    setAuthoritativeIndicatorSlot(oiSlot, makeSeries(), 1, "zh");
    setAuthoritativeIndicatorSlot(
      emaSlot,
      makeSeries({
        name: "EMA",
        pane: "main",
        params: { period: 9 },
        points: [
          { timestamp: 1_000, values: { value: "101" } },
          { timestamp: 2_000, values: { value: "105" } },
        ],
      }),
      1,
      "zh",
    );

    const engine = createAuthoritativeIndicatorEngine();
    expect(engine.language).toBe(TALARIA_INDICATOR_LANGUAGE);

    const oiPrepared = await engine.prepare(oiSlot, "ind-oi");
    expect(oiPrepared.inputs).toEqual([]);
    expect(oiPrepared.meta).toEqual({ title: "OI 背离(30)", overlay: false });

    const emaPrepared = await engine.prepare(emaSlot, "ind-ema");
    expect(emaPrepared.inputs).toEqual([]);
    expect(emaPrepared.meta).toEqual({ title: "EMA(9)", overlay: true });

    let latestModel: IndicatorModel | null = null;
    let currentBars = BARS;
    const onModel = vi.fn((model: IndicatorModel) => {
      latestModel = model;
    });

    const session = engine.execute(
      {
        prepared: oiPrepared,
        market: { symbol: "BTCUSDT", timeframe: "5m" },
        bars: currentBars,
        getBars: () => currentBars,
        inputs: { slotKey: oiSlot, version: 1 },
        props: {},
        mode: "live",
      },
      {
        onModel,
        onAlert: vi.fn(),
        onWarning: vi.fn(),
        onError: vi.fn(),
      },
    );

    expect(onModel).toHaveBeenCalledTimes(1);
    expect(latestModel).not.toBeNull();
    expect(latestModel!.title).toBe("OI 背离(30)");
    expect(latestModel!.shorttitle).toBe("OI 背离(30)");
    expect(latestModel!.title).not.toBe("Talaria Study");
    expect(latestModel!.anchorTime).toBe(1_000);
    expect(latestModel!.overlay).toBe(false);
    expect(latestModel!.paneHint).toBe("new");
    expect(latestModel!.paneId).toBe("ind-oi");
    expect(latestModel!.series).toHaveLength(1);
    const firstSeries = latestModel!.series[0] as LineLikeSeries;
    expect(firstSeries.title).toBe("OI 背离(30)");
    expect(firstSeries.points).toEqual([
      { time: 1_000, value: null },
      { time: 2_000, value: 0.0123 },
    ]);

    expect(setAuthoritativeIndicatorLocale(oiSlot, "en")).toBe(true);
    session.update({ slotKey: oiSlot, version: 2 });
    expect(latestModel!.title).toBe("OI divergence(30)");
    expect(latestModel!.shorttitle).toBe("OI divergence(30)");

    setAuthoritativeIndicatorSlot(
      oiSlot,
      makeSeries({
        params: { period: 21 },
        points: [
          { timestamp: 1_000, values: { value: null } },
          { timestamp: 2_000, values: { value: "0.02" } },
          { timestamp: 3_000, values: { value: "-0.01" } },
        ],
      }),
      3,
      "en",
    );
    currentBars = [...BARS, { time: 3_000, open: 107, high: 109, low: 104, close: 106 }];
    session.notifyBars();
    expect(latestModel!.title).toBe("OI divergence(21)");
    expect((latestModel!.series[0] as LineLikeSeries).points).toEqual([
      { time: 1_000, value: null },
      { time: 2_000, value: 0.02 },
      { time: 3_000, value: -0.01 },
    ]);
  });
});
