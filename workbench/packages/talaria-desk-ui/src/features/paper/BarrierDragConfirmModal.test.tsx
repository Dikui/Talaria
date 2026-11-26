import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { classifyBarrierDrag, type BarrierDragProposal } from "../../chart/barrierDragController";
import { setLocale } from "../../lib/i18n";
import { BarrierDragConfirmModal } from "./BarrierDragConfirmModal";

function makeProposal(overrides: Partial<BarrierDragProposal> = {}): BarrierDragProposal {
  const classification = classifyBarrierDrag(
    {
      round_id: "round-btc-1",
      sub_account_id: "sa-alpha",
      direction: "LONG",
      stop: "49000",
      take_profit: ["51500"],
      entry_price: "50000",
      quantity: "0.5",
      ledger_seq: 18,
    },
    "STOP",
    -1,
    "50050",
  );
  return {
    roundId: "round-btc-1",
    subAccountId: "sa-alpha",
    direction: "LONG",
    kind: "STOP",
    tpIndex: -1,
    originalPrice: "49000",
    targetPrice: "50050",
    entryPrice: "50000",
    basedOnLedgerSeq: 18,
    classification,
    payload: { stop: "50050" },
    ...overrides,
  };
}

describe("Ticket 08: BarrierDragConfirmModal (spec D10)", () => {
  beforeEach(() => {
    setLocale("zh");
  });

  it("shows old->new price, R delta, breakeven badge, and reverts with zero submit on Cancel", () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();

    render(
      <BarrierDragConfirmModal
        proposal={makeProposal()}
        onCancel={onCancel}
        onConfirm={onConfirm}
      />,
    );

    expect(screen.getByTestId("barrier-drag-confirm-dialog")).toBeTruthy();
    expect(screen.getByTestId("barrier-drag-price-diff").textContent).toContain("49000");
    expect(screen.getByTestId("barrier-drag-price-diff").textContent).toContain("50050");
    expect(screen.getByTestId("barrier-drag-breakeven-badge").textContent).toContain("保本");
    expect(screen.getByTestId("barrier-drag-direct-badge")).toBeTruthy();

    fireEvent.click(screen.getByTestId("barrier-drag-cancel"));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("shows risk approval warning for widening stop and passes optional reason on Confirm", () => {
    setLocale("en");
    const widenClass = classifyBarrierDrag(
      {
        round_id: "round-btc-1",
        sub_account_id: "sa-alpha",
        direction: "LONG",
        stop: "49000",
        take_profit: ["51500"],
        entry_price: "50000",
        quantity: "0.5",
        ledger_seq: 18,
      },
      "STOP",
      -1,
      "48500",
    );
    const proposal = makeProposal({
      targetPrice: "48500",
      classification: widenClass,
      payload: { stop: "48500" },
    });

    const onCancel = vi.fn();
    const onConfirm = vi.fn();

    render(
      <BarrierDragConfirmModal
        proposal={proposal}
        onCancel={onCancel}
        onConfirm={onConfirm}
      />,
    );

    expect(screen.getByTestId("barrier-drag-risk-badge").textContent).toContain("Requires risk approval");
    expect(screen.getByTestId("barrier-drag-r-delta").textContent).toContain("+0.50 R");

    fireEvent.change(screen.getByTestId("barrier-drag-reason-input"), {
      target: { value: "give structure room" },
    });
    fireEvent.click(screen.getByTestId("barrier-drag-submit"));

    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onConfirm).toHaveBeenCalledWith(proposal, "give structure room");
    expect(onCancel).not.toHaveBeenCalled();
  });
});
