import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { DraftRecord } from "@talaria/contracts";

import { api } from "../../lib/api";
import { setLocale } from "../../lib/i18n";
import { DraftPanel } from "./DraftPanel";
import {
  clearLastSubAccount,
  getLastSubAccount,
  resolveDefaultSubAccount,
  setLastSubAccount,
} from "./useLastSubAccount";

afterEach(() => {
  setLocale("zh");
  clearLastSubAccount();
  vi.restoreAllMocks();
});

function sampleDraft(overrides: Partial<DraftRecord> = {}): DraftRecord {
  return {
    draft_id: "drf_chart_1",
    version: 1,
    content_hash: "b".repeat(64),
    status: "STAGED",
    claimed: false,
    receipt: null,
    content: {
      sub_account_id: "sa-alpha",
      instrument_id: "BTC-USDT-PERP",
      snapshot_id: "snap_chart_1",
      direction: "LONG",
      entry_low: "68000.0",
      entry_high: "68000.0",
      invalidation_stop: "67600.0",
      take_profit: ["68800.0"],
      valid_until: null,
      consultation_id: null,
      round_id: null,
      inherited_barriers: null,
    },
    ...overrides,
  };
}

describe("Ticket 07: chart-seeded DraftForm, last-used sub-account (D8), and DraftPanel highlight", () => {
  it("persists and resolves last-used sub-account in localStorage (spec D8)", () => {
    expect(getLastSubAccount()).toBe("");
    expect(resolveDefaultSubAccount(["sa-1", "sa-2"])).toBe("sa-1");

    setLastSubAccount("sa-2");
    expect(getLastSubAccount()).toBe("sa-2");
    expect(resolveDefaultSubAccount(["sa-1", "sa-2"])).toBe("sa-2");
    expect(resolveDefaultSubAccount(["sa-1", "sa-2"], "sa-explicit")).toBe("sa-explicit");
  });

  it("opens pre-filled DraftForm from externalSeed (single entry + TP1 -> editable band & multi-TP) and notifies on save", async () => {
    setLastSubAccount("sa-remembered");
    const savedDraft = sampleDraft({
      content: {
        ...sampleDraft().content,
        sub_account_id: "sa-remembered",
        entry_low: "68000.0",
        entry_high: "68050.0",
        take_profit: ["68800.0", "69200.0"],
      },
    });

    vi.spyOn(api, "paperDrafts").mockResolvedValue([]);
    vi.spyOn(api, "paperAccounts").mockResolvedValue(["sa-default", "sa-remembered"]);
    vi.spyOn(api, "paperBarriers").mockResolvedValue([]);
    vi.spyOn(api, "previewPaperSizing").mockResolvedValue({
      quantity: "0.050",
      basis: "risk_budget=50",
      risk_amount_r: "50",
      risk_pct_of_equity: "0.005",
      est_liq_price: "55000",
      est_liq_distance: "12600",
      est_liq_distance_pct: "0.185",
      rejected_reason: null,
      from_offline_snapshot: false,
      based_on_ledger_seq: 12,
    });
    const saveSpy = vi.spyOn(api, "savePaperDraft").mockResolvedValue(savedDraft);
    const onConsumed = vi.fn();
    const onMutated = vi.fn();

    render(
      <DraftPanel
        activeInstrumentId="BTC-USDT-PERP"
        activeTimeframe="1m"
        pollIntervalMs={0}
        externalSeed={{
          nonce: 101,
          instrument_id: "BTC-USDT-PERP",
          snapshot_id: "snap_chart_1",
          direction: "LONG",
          entry_low: "68000.0",
          entry_high: "68000.0",
          invalidation_stop: "67600.0",
          take_profit: ["68800.0"],
        }}
        onExternalSeedConsumed={onConsumed}
        onDraftsMutated={onMutated}
      />,
    );

    await waitFor(() => {
      expect(onConsumed).toHaveBeenCalledWith(101);
    });

    // Sub-account defaults to remembered last-used sub-account (spec D8)
    const acctInput = screen.getByLabelText("子账户") as HTMLInputElement;
    expect(acctInput.value).toBe("sa-remembered");

    // Expand entry_high and add TP2 in form
    const entryHighInput = screen.getByLabelText("入场上界") as HTMLInputElement;
    const tpInput = screen.getByLabelText("止盈（逗号分隔）") as HTMLInputElement;
    fireEvent.change(entryHighInput, { target: { value: "68050.0" } });
    fireEvent.change(tpInput, { target: { value: "68800.0, 69200.0" } });

    fireEvent.click(screen.getByRole("button", { name: "保存草案" }));

    await waitFor(() => {
      expect(saveSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          sub_account_id: "sa-remembered",
          snapshot_id: "snap_chart_1",
          entry_low: "68000.0",
          entry_high: "68050.0",
          invalidation_stop: "67600.0",
          take_profit: ["68800.0", "69200.0"],
        }),
      );
      expect(onMutated).toHaveBeenCalled();
    });
  });

  it("highlights and scrolls to the draft clicked on the chart LAYER_STAGED_DRAFT", async () => {
    const d1 = sampleDraft({ draft_id: "drf_1" });
    const d2 = sampleDraft({ draft_id: "drf_2" });
    vi.spyOn(api, "paperDrafts").mockResolvedValue([d1, d2]);

    render(
      <DraftPanel
        activeInstrumentId="BTC-USDT-PERP"
        activeTimeframe="1m"
        pollIntervalMs={0}
        highlightedDraftId="drf_2"
      />,
    );

    const card2 = await screen.findByTestId("draft-card-drf_2");
    const card1 = screen.getByTestId("draft-card-drf_1");
    expect(card2.dataset.highlighted).toBe("true");
    expect(card2.className).toContain("is-highlighted");
    expect(card1.dataset.highlighted).toBeUndefined();
  });
});
