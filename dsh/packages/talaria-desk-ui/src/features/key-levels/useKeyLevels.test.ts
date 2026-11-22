import { describe, expect, it } from "vitest";

import type { KeyLevelSet } from "@talaria/contracts";

import { selectKeyLevelSet } from "./useKeyLevels";

const scope = { instrumentId: "BTC-USDT-PERP", timeframe: "1m" };

const chart: KeyLevelSet = {
  snapshot_id: "chart",
  instrument_id: scope.instrumentId,
  timeframe: scope.timeframe,
  cutoff_ts: 100,
  as_of_ts: 100,
  levels: [],
};

const live = { ...chart, snapshot_id: "live", cutoff_ts: 200, as_of_ts: 200 };

describe("chart key level versions", () => {
  it("accepts a newer snapshot pushed for the open instrument and timeframe", () => {
    expect(selectKeyLevelSet(chart, live, scope, "stream")).toBe(live);
  });

  it.each(["rest", "stream"] as const)("keeps the latest cutoff when an older %s version arrives", (source) => {
    expect(selectKeyLevelSet(live, chart, scope, source)).toBe(live);
  });

  it.each([{ instrument_id: "ETH-USDT-PERP" }, { timeframe: "5m" }])(
    "ignores pushes from a different chart series (%j)", (other) => {
      expect(selectKeyLevelSet(chart, { ...live, ...other }, scope, "stream")).toBe(chart);
    },
  );

  it("accepts a same-cutoff replacement push and keeps it when REST completes", () => {
    const replacement = { ...live, snapshot_id: "replacement" };
    expect(selectKeyLevelSet(live, replacement, scope, "stream")).toBe(replacement);
    expect(selectKeyLevelSet(replacement, { ...live, snapshot_id: "chart" }, scope, "rest")).toBe(replacement);
  });
});
