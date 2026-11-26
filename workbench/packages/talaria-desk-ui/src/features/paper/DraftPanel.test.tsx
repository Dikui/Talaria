import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { DraftRecord } from "@talaria/contracts";

import { api } from "../../lib/api";
import { setLocale } from "../../lib/i18n";
import { DraftForm } from "./DraftForm";
import { DraftPanel, DraftReview } from "./DraftPanel";

afterEach(() => {
  setLocale("zh");
  vi.restoreAllMocks();
});

function sampleDraft(overrides: Partial<DraftRecord> = {}): DraftRecord {
  return {
    draft_id: "drf_1",
    version: 1,
    content_hash: "a".repeat(64),
    status: "STAGED",
    claimed: false,
    receipt: null,
    content: {
      sub_account_id: "sa-paper",
      instrument_id: "BTC-USDT-PERP",
      snapshot_id: "snap_1",
      direction: "LONG",
      entry_low: "64500",
      entry_high: "64600",
      invalidation_stop: "64000",
      take_profit: ["66000"],
      valid_until: null,
      consultation_id: null,
      round_id: null,
      inherited_barriers: null,
    },
    ...overrides,
  };
}

describe("DraftPanel and DraftReview", () => {
  it("renders empty state in Chinese and English", async () => {
    vi.spyOn(api, "paperDrafts").mockResolvedValue([]);

    const { rerender } = render(
      <DraftPanel
        activeInstrumentId="BTC-USDT-PERP"
        activeTimeframe="1m"
        pollIntervalMs={0}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("paper-drafts-empty")).toBeTruthy();
    });
    expect(screen.getByText(/暂无暂存交易草案/)).toBeTruthy();

    setLocale("en-US");
    rerender(
      <DraftPanel
        activeInstrumentId="BTC-USDT-PERP"
        activeTimeframe="1m"
        pollIntervalMs={0}
      />,
    );
    expect(screen.getByText(/No staged plan drafts yet/)).toBeTruthy();
  });

  it("displays visible error banner when draft channel fails (404 / offline)", async () => {
    vi.spyOn(api, "paperDrafts").mockRejectedValue(new Error("HTTP 404 NOT_FOUND"));

    render(
      <DraftPanel
        activeInstrumentId="BTC-USDT-PERP"
        activeTimeframe="1m"
        pollIntervalMs={0}
      />,
    );

    const banner = await screen.findByTestId("paper-drafts-error");
    expect(banner.textContent).toContain("HTTP 404 NOT_FOUND");
  });

  it("creates, confirms, and discards drafts through the API", async () => {
    const staged = sampleDraft();
    const listSpy = vi.spyOn(api, "paperDrafts").mockResolvedValue([staged]);
    const confirmSpy = vi
      .spyOn(api, "confirmPaperDraft")
      .mockResolvedValue({ ...staged, status: "CONFIRMED" });
    const discardSpy = vi
      .spyOn(api, "discardPaperDraft")
      .mockResolvedValue({ ...staged, status: "DISCARDED" });

    render(
      <DraftPanel
        activeInstrumentId="BTC-USDT-PERP"
        activeTimeframe="1m"
        pollIntervalMs={0}
      />,
    );

    await screen.findByTestId("draft-card-drf_1");

    // Confirm
    fireEvent.click(screen.getByRole("button", { name: "确认并提交模拟盘" }));
    await waitFor(() => {
      expect(confirmSpy).toHaveBeenCalledWith("drf_1", {
        version: 1,
        content_hash: "a".repeat(64),
        confirmed_by: "trader",
      });
    });

    // Discard
    fireEvent.click(screen.getByRole("button", { name: "丢弃草案" }));
    await waitFor(() => {
      expect(discardSpy).toHaveBeenCalledWith("drf_1", { version: 1 });
    });
    expect(listSpy).toHaveBeenCalled();
  });

  it("renders DISCARDED, EXPIRED, DRAFT_STALE, risk rejection, and long instrument names cleanly", () => {
    const longInstrument =
      "SUPER-LONG-PERPETUAL-CONTRACT-INSTRUMENT-SETTLEMENT-NAME-BTC-USDT-PERP-2026";
    const longReject = sampleDraft({
      draft_id: "drf_rej",
      status: "CONFIRMED",
      claimed: true,
      content: {
        ...sampleDraft().content,
        instrument_id: longInstrument,
      },
      receipt: {
        draft_id: "drf_rej",
        version: 1,
        accepted: false,
        reason: "MAX_SINGLE_R_EXCEEDED_WITH_DETAILED_EXPLANATION_FOR_OVERFLOW_CHECK",
      },
    });
    const stale = sampleDraft({
      draft_id: "drf_stale",
      status: "CONFIRMED",
      claimed: true,
      receipt: {
        draft_id: "drf_stale",
        version: 1,
        accepted: false,
        reason: "DRAFT_STALE",
      },
    });
    const discarded = sampleDraft({ draft_id: "drf_disc", status: "DISCARDED" });
    const expired = sampleDraft({ draft_id: "drf_exp", status: "EXPIRED" });

    render(
      <div>
        <DraftReview draft={longReject} refresh={() => {}} />
        <DraftReview draft={stale} refresh={() => {}} />
        <DraftReview draft={discarded} refresh={() => {}} />
        <DraftReview draft={expired} refresh={() => {}} />
      </div>,
    );

    expect(screen.getByText(longInstrument)).toBeTruthy();
    expect(
      screen.getByText(/MAX_SINGLE_R_EXCEEDED_WITH_DETAILED_EXPLANATION_FOR_OVERFLOW_CHECK/),
    ).toBeTruthy();
    expect(screen.getByText(/DRAFT_STALE/)).toBeTruthy();
    expect(screen.getByText("已丢弃")).toBeTruthy();
    expect(screen.getByText("已过期")).toBeTruthy();
    // Terminal drafts must not expose confirm/edit/discard buttons
    expect(screen.queryByRole("button", { name: "确认并提交模拟盘" })).toBeNull();
    expect(screen.queryByRole("button", { name: "丢弃草案" })).toBeNull();
  });

  it("renders live sizing preview, offline snapshot badge, long rejection reason, and preview-vs-actual diff", async () => {
    vi.spyOn(api, "paperAccounts").mockResolvedValue(["sa-live"]);
    vi.spyOn(api, "paperBarriers").mockResolvedValue([]);
    const previewSpy = vi.spyOn(api, "previewPaperSizing").mockResolvedValue({
      quantity: "0.068",
      basis: "risk_budget=50, worst_entry=64600, stop=64000, risk_per_unit=729.2",
      risk_amount_r: "49.5856",
      risk_pct_of_equity: "0.00495856",
      est_liq_price: "51939.69849246",
      est_liq_distance: "12060.30150754",
      est_liq_distance_pct: "0.18669197",
      rejected_reason:
        "CUSTOM_LONG_REJECTION_CODE_EXPLAINING_MARGIN_AND_RISK_CONSTRAINTS_IN_DETAIL",
      from_offline_snapshot: true,
      based_on_ledger_seq: 17,
    });

    render(
      <DraftForm
        seed={{
          sub_account_id: "sa-live",
          instrument_id: "BTC-USDT-PERP",
          snapshot_id: "snap_1",
          direction: "LONG",
          entry_low: "64500",
          entry_high: "64600",
          invalidation_stop: "64000",
          take_profit: ["66000"],
        }}
        previewDebounceMs={0}
        onClose={() => {}}
        onSaved={() => {}}
      />,
    );

    await waitFor(() => {
      expect(previewSpy).toHaveBeenCalled();
      expect(screen.getByTestId("paper-sizing-qty").textContent).toBe("0.068");
    });
    expect(screen.getByTestId("paper-sizing-offline-badge").textContent).toContain("基于离线快照");
    expect(screen.getByTestId("paper-sizing-rejection").textContent).toContain(
      "CUSTOM_LONG_REJECTION_CODE_EXPLAINING_MARGIN_AND_RISK_CONSTRAINTS_IN_DETAIL",
    );
    expect(screen.getByTestId("paper-sizing-basis").textContent).toContain("risk_budget=50");

    // Verify preview-vs-actual comparison on DraftReview when receipt quantity differs
    const executed = sampleDraft({
      draft_id: "drf_diff",
      status: "CONFIRMED",
      claimed: true,
      receipt: {
        draft_id: "drf_diff",
        version: 1,
        accepted: true,
        plan_id: "draft-drf_diff-v1",
        quantity: "0.066",
      },
    });
    render(<DraftReview draft={executed} previewedQuantity="0.068" refresh={() => {}} />);
    expect(screen.getByTestId("paper-sizing-qty-diff").textContent).toContain("0.068");
  });
});
