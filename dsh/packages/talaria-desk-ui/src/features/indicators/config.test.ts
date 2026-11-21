import { describe, expect, it } from "vitest";

import {
  DEFAULT_ENABLED,
  INDICATOR_DEFAULTS,
  applyDefaults,
  defaultIndicatorSettings,
  defaultsFromParams,
  enabledIndicatorNames,
  needsOperatorRequest,
  operatorParams,
  settingsSignature,
} from "./config";

describe("indicator config", () => {
  it("starts with EMA/RSI/ATR enabled and backend-compatible defaults", () => {
    const settings = defaultIndicatorSettings();
    expect(DEFAULT_ENABLED).toEqual(["EMA", "RSI", "ATR"]);
    expect(enabledIndicatorNames(settings)).toEqual(["EMA", "RSI", "ATR"]);
    expect(needsOperatorRequest(enabledIndicatorNames(settings), settings)).toBe(false);
  });

  it("aligns EMA/SMA defaults with Pine v6 and the backend", () => {
    expect(INDICATOR_DEFAULTS.EMA.period).toBe(9);
    expect(INDICATOR_DEFAULTS.SMA.period).toBe(9);
    expect(INDICATOR_DEFAULTS.VWAP.anchor).toBe("session");
    const settings = defaultIndicatorSettings();
    expect(settings.EMA.period).toBe(9);
    expect(needsOperatorRequest(["EMA"], settings)).toBe(false);
    settings.EMA.period = 20;
    expect(needsOperatorRequest(["EMA"], settings)).toBe(true);
  });

  it("initializes settings from effective server defaults", () => {
    const defaults = defaultsFromParams({
      EMA: { period: 30 },
      VWAP: { period: 1, anchor: "session" },
    });
    expect(defaults.EMA.period).toBe(30);
    expect(defaults.VWAP.anchor).toBe("session");
    expect(defaults.RSI.period).toBe(14);

    const settings = applyDefaults(defaultIndicatorSettings(), defaults);
    expect(settings.EMA.period).toBe(30);
    expect(settings.EMA.enabled).toBe(true);
    expect(needsOperatorRequest(["EMA"], settings, defaults)).toBe(false);
    settings.EMA.period = 9;
    expect(needsOperatorRequest(["EMA"], settings, defaults)).toBe(true);
  });

  it("switches to operator requests when a period or BOLL multiplier changes", () => {
    const settings = defaultIndicatorSettings();
    settings.BOLL.enabled = true;
    expect(needsOperatorRequest(["BOLL"], settings)).toBe(false);
    settings.BOLL.stdDev = "2.5";
    expect(needsOperatorRequest(["BOLL"], settings)).toBe(true);
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
    expect(needsOperatorRequest(["FUNDING_QUANTILE"], settings)).toBe(false);
    expect(operatorParams("FUNDING_QUANTILE", settings.FUNDING_QUANTILE)).toEqual({
      period: 30,
    });
  });

  it("sends VWAP anchor and includes it in the refetch signature", () => {
    const settings = defaultIndicatorSettings();
    settings.VWAP.enabled = true;
    expect(needsOperatorRequest(["VWAP"], settings)).toBe(false);
    settings.VWAP.anchor = "snapshot";
    expect(operatorParams("VWAP", settings.VWAP)).toEqual({
      period: 1,
      anchor: "snapshot",
    });
    expect(needsOperatorRequest(["VWAP"], settings)).toBe(true);
    expect(settingsSignature(["VWAP"], settings)).toContain("VWAP:1::snapshot");
  });
});
