import { describe, expect, it } from "vitest";

import {
  DEFAULT_ENABLED,
  defaultIndicatorSettings,
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

  it("sends VWAP anchor and includes it in the refetch signature", () => {
    const settings = defaultIndicatorSettings();
    settings.VWAP.enabled = true;
    settings.VWAP.anchor = "session";
    expect(operatorParams("VWAP", settings.VWAP)).toEqual({
      period: 1,
      anchor: "session",
    });
    expect(needsOperatorRequest(["VWAP"], settings)).toBe(true);
    expect(settingsSignature(["VWAP"], settings)).toContain("VWAP:1::session");
  });
});
