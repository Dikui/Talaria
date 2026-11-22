import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { PaperRound } from "@talaria/contracts";
import { DraftReview, LedgerSections } from "./AccountPanel";

it("keeps closed rounds and their long instrument name visible with no positions", () => {
  const round: PaperRound = {
    round_id: "first", sub_account_id: "test", instrument_id: "VERY-LONG-EXCHANGE-SETTLEMENT-INSTRUMENT-NAME",
    direction: "SHORT", quantity: "0", stop: "150", take_profit: ["125"], phase: "CLOSED",
  };
  const html = renderToStaticMarkup(<LedgerSections view={{
    sub_account_id: "test", positions: [], rounds: [round, { ...round, round_id: "second" }],
    fills: [{ event_id: "event-1", round_id: "first", instrument_id: round.instrument_id,
      kind: "EXIT_FILLED", ts_event: 1_795_022_974_103, quantity: "0.00001", price: "125.001" }],
  }} />);
  expect(html).toContain("当前无持仓");
  expect(html.match(/已平仓/g)).toHaveLength(2);
  expect(html).toContain(round.instrument_id);
  expect(html).toContain("0.00001");
  expect(html).toContain("125.001");
});

it("shows the immutable evidence and deadline being confirmed", () => {
  const html = renderToStaticMarkup(<DraftReview refresh={() => {}} draft={{
    draft_id: "draft", version: 2, content_hash: "reviewed-hash", status: "MODIFIED", claimed: false,
    content: {sub_account_id: "one", instrument_id: "BTC", snapshot_id: "frozen-evidence",
      valid_until: 120000, direction: "LONG", entry_low: "10", entry_high: "11",
      invalidation_stop: "9", take_profit: ["13"]},
  }} />);
  expect(html).toContain('name="content_hash" value="reviewed-hash"');
  expect(html).toContain('name="version" value="2"');
  expect(html).toContain("frozen-evidence");
  expect(html).toContain(new Date(120000).toISOString());
});
