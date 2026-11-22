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
    condition_true: [{ timestamp: 120_000, observed_at: 180_010, price: "42.5" }],
    would_trigger: [{ timestamp: 120_000, observed_at: 180_010, price: "42.5" }],
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
  const markers = configs.find((config) => config.id === "talaria-rule-condition_true")!;
  expect(configs.some((config) => config.id === "talaria-rule-would_trigger")).toBe(true);
  const [line] = configs.filter((config) => config.id !== markers.id);
  expect(markers.calc!(data, undefined as never)).toBeInstanceOf(Promise);
  expect(await markers.calc!(data, undefined as never)).toEqual([
    { signal: null }, { signal: 42.5 }, { signal: null },
  ]);
  expect(await line.calc!(data, undefined as never)).toEqual([
    { value: null, index: 0 }, { value: 40.25, index: 1 }, { value: null, index: -1 },
  ]);
});


it("ends a rule line at a FEED_GAP omitted from the K-line data list", async () => {
  const [line] = ruleChartIndicators({ rule: { version: "1", timing: "bar_close", expression: {
    op: "compare", test: "gt", left: { source: "bar", field: "close" }, right: { source: "constant", value: "0" },
  } }, condition_true: [], would_trigger: [], indicators: [{
    operand: { source: "indicator", name: "SMA" }, pane: "main", points: [
      { timestamp: 0, values: { value: "1" } }, { timestamp: 60000, values: { value: null } },
      { timestamp: 120000, values: { value: "3" } }, { timestamp: 180000, values: { value: "4" } },
    ],
  }] }, "blue");
  const data = [0, 120000, 180000].map(timestamp => ({ timestamp, open: 1, high: 2, low: 1, close: 2 }));
  const rows = await line.calc!(data, undefined as never);
  const attrs = line.figures![0].attrs!({ data: { current: rows[0], next: rows[1] },
    coordinate: { current: { x: 1, value: 1 }, next: { x: 2, value: 3 } } } as never);
  expect(attrs?.coordinates).toEqual([{ x: 1, y: 1 }, { x: 1, y: 1 }]);
});
