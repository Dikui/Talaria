import type { RulePreviewResponse } from "@talaria/contracts";
import type { KLineData } from "klinecharts";
import { expect, it } from "vitest";
import { ruleChartIndicators } from "./ruleChart";

// alerts-and-scanning/02: the chart consumes async calc output by K-line timestamp.
it("aligns server markers and indicator values with the chart without filling warmup bars", async () => {
  const reference = {
    source: "indicator", name: "SMA", channel: "value", params: { period: 20 },
  } as const;
  const output: RulePreviewResponse = {
    rule: {
      version: "1", timing: "bar_close",
      expression: { op: "compare", test: "gt", left: reference, right: { source: "constant", value: "0" } },
    },
    signals: [{ timestamp: 120_000, price: "42.5" }],
    indicators: [{
      operand: reference, pane: "main", points: [
        { timestamp: 60_000, values: { value: null } },
        { timestamp: 120_000, values: { value: "40.25" } },
      ],
    }],
  };
  const configs = ruleChartIndicators(output, "light");
  const data: KLineData[] = [60_000, 120_000, 180_000].map((timestamp) => ({
    timestamp, open: 42, high: 43, low: 41, close: 42,
  }));
  const markers = configs.find((config) => config.id === "talaria-rule-signals")!;
  const [line] = configs.filter((config) => config.id !== markers.id);
  expect(markers.calc!(data, undefined as never)).toBeInstanceOf(Promise);
  expect(await markers.calc!(data, undefined as never)).toEqual([
    { signal: null }, { signal: 42.5 }, { signal: null },
  ]);
  expect(await line.calc!(data, undefined as never)).toEqual([
    { value: null }, { value: 40.25 }, { value: null },
  ]);
});
