import { afterEach, describe, expect, it } from "vitest";

import { setLocale } from "../../lib/i18n";

import {
  DEFAULT_ENABLED,
  INDICATOR_DEFAULTS,
  defaultIndicatorSettings,
  defaultsFromParams,
  enabledIndicatorNames,
  operatorParams,
  indicatorRequests,
  indicatorLabel,
  anchorLabel,
  updateIndicatorSetting,
} from "./config";

describe("indicator config", () => {
  it("starts with EMA/RSI/ATR enabled and backend-compatible defaults", () => {
    const settings = defaultIndicatorSettings();
    expect(DEFAULT_ENABLED).toEqual(["EMA", "RSI", "ATR"]);
    expect(enabledIndicatorNames(settings)).toEqual(["EMA", "RSI", "ATR"]);
  });

  it("aligns EMA/SMA defaults with Pine v6 and the backend", () => {
    expect(INDICATOR_DEFAULTS.EMA.period).toBe(9);
    expect(INDICATOR_DEFAULTS.SMA.period).toBe(9);
    expect(INDICATOR_DEFAULTS.VWAP.anchor).toBe("session");
    const settings = defaultIndicatorSettings();
    expect(settings.EMA.period).toBe(9);
  });

  it("initializes settings from effective server defaults", () => {
    const defaults = defaultsFromParams({
      EMA: { period: 30 },
      VWAP: { period: 1, anchor: "session" },
    });
    expect(defaults.EMA.period).toBe(30);
    expect(defaults.VWAP.anchor).toBe("session");
    expect(defaults.RSI.period).toBe(14);

    const settings = defaultIndicatorSettings(defaults);
    expect(settings.EMA.period).toBe(30);
    expect(settings.EMA.enabled).toBe(true);
  });

  it("sends the instance BOLL multiplier with its period", () => {
    const settings = defaultIndicatorSettings();
    settings.BOLL.enabled = true;
    settings.BOLL.stdDev = "2.5";
    expect(operatorParams("BOLL", settings.BOLL)).toEqual({
      period: 20,
      std_dev: "2.5",
    });
  });

  it("exposes funding quantile and OI divergence with backend defaults", () => {
    const settings = defaultIndicatorSettings();
    expect(settings.FUNDING_QUANTILE.period).toBe(30);
    expect(settings.OI_DIVERGENCE.period).toBe(30);
    settings.FUNDING_QUANTILE.enabled = true;
    expect(operatorParams("FUNDING_QUANTILE", settings.FUNDING_QUANTILE)).toEqual({
      period: 30,
    });
  });

  it("sends the instance VWAP anchor", () => {
    const settings = defaultIndicatorSettings();
    settings.VWAP.enabled = true;
    settings.VWAP.anchor = "snapshot";
    expect(operatorParams("VWAP", settings.VWAP)).toEqual({
      period: 1,
      anchor: "snapshot",
    });
  });
});


describe("indicator instances", () => {
  it("waits for defaults before requesting chart instances and pins explicit parameters", () => {
    const defaults = defaultsFromParams({ EMA: { period: 30 } });
    const settings = defaultIndicatorSettings(defaults);
    expect(settings.EMA.period).toBe(30);
    expect(indicatorRequests(null, "snapshot", "1m", 300)).toEqual([]);
    expect(indicatorRequests(settings, "snapshot", "1m", 300)[0]).toEqual({
      snapshot_id: "snapshot",
      timeframe: "1m",
      operator: "EMA",
      params: { period: 30 },
      limit: 300,
    });
  });

  it("uses saved or cleared defaults only when enabling a new instance", () => {
    const open = defaultIndicatorSettings(INDICATOR_DEFAULTS);
    const saved = defaultsFromParams({ EMA: { period: 22 }, BOLL: { period: 11, std_dev: "3" } });
    const added = updateIndicatorSetting(open, saved, "BOLL", { enabled: true });
    expect(operatorParams("EMA", added.EMA)).toEqual({ period: 9 });
    expect(operatorParams("BOLL", added.BOLL)).toEqual({ period: 11, std_dev: "3" });

    const off = updateIndicatorSetting(added, INDICATOR_DEFAULTS, "BOLL", { enabled: false });
    const reopened = updateIndicatorSetting(off, INDICATOR_DEFAULTS, "BOLL", { enabled: true });
    expect(operatorParams("BOLL", added.BOLL)).toEqual({ period: 11, std_dev: "3" });
    expect(operatorParams("BOLL", reopened.BOLL)).toEqual({ period: 20, std_dev: "2" });
  });

  it("exposes NET_ODDS and initializes new instances from its saved period", () => {
    const defaults = defaultsFromParams({ NET_ODDS: { period: 28 } });
    const settings = defaultIndicatorSettings(defaults);
    expect(settings.NET_ODDS).toEqual({ enabled: false, period: 28, stdDev: "2", anchor: "snapshot" });
    const enabled = updateIndicatorSetting(settings, defaults, "NET_ODDS", { enabled: true });
    expect(enabledIndicatorNames(enabled)).toContain("NET_ODDS");
    expect(operatorParams("NET_ODDS", enabled.NET_ODDS)).toEqual({ period: 28 });
  });
});

describe("indicator labels (English)", () => {
  afterEach(() => setLocale("zh"));

  it("localizes indicator names and anchor labels", () => {
    setLocale("en");
    expect(indicatorLabel("FUNDING_QUANTILE")).toBe("Funding rate quantile");
    expect(anchorLabel("session")).toBe("UTC day boundary");
    expect(anchorLabel("snapshot")).toBe("Snapshot start");
    setLocale("zh");
    expect(indicatorLabel("FUNDING_QUANTILE")).toBe("资金费率分位");
    expect(anchorLabel("session")).toBe("UTC 日界");
  });
});
