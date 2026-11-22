import { expect, it } from "vitest";
import { newComparison, expressionComplete, indicatorOperand } from "./ruleForm";
import { INDICATOR_DEFAULTS } from "../indicators/config";
import { RuleDrafts, savedAlertExists, ruleDrawingOptions } from "./ruleDrafts";
import type { DrawingRecord } from "@talaria/contracts";

it("restores close and event drafts for each linked instrument and timeframe", () => {
  const drafts = new RuleDrafts();
  const close = newComparison("bar_close");
  const event = newComparison("event");
  drafts.write("red/BTC/1m", { timing: "event", expressions: { bar_close: close, event } });
  for (const scope of ["red/ETH/1m", "red/BTC/5m", "blue/BTC/1m"]) {
    expect(drafts.read(scope).expressions).toEqual({ event: null, bar_close: null });
  }
  expect(drafts.read("red/BTC/1m")).toEqual({ timing: "event", expressions: { bar_close: close, event } });
});

it("previews a selected historical level after it leaves the current version", () => {
  expect(expressionComplete({ op: "level_event", level_id: "historical", event: "TOUCH" }, new Set())).toBe(true);
});

it("offers persisted drawings outside the viewport and excludes optimistic ids", () => {
  const drawing = { drawing_id: "offscreen", version: 1, kind: "HORIZONTAL_LINE", instrument_id: "BTC" } as DrawingRecord;
  expect(ruleDrawingOptions([drawing, { ...drawing, drawing_id: "pending_1", version: 0 }], "BTC")).toEqual([drawing]);
});

it("allows saving the same rule again once its alert was deleted", () => {
  expect(savedAlertExists("a", [{ alert_id: "a" }])).toBe(true);
  expect(savedAlertExists("a", [{ alert_id: "b" }])).toBe(false);
});

it("creates chart-window operands for chart indicators", () => {
  expect(indicatorOperand("VWAP", INDICATOR_DEFAULTS).window).toBe("chart");
});
