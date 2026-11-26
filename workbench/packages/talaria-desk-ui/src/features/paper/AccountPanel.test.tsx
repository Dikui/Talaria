import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PaperAccountView } from "@talaria/contracts";

import { roundsToBarriers } from "../../chart/adapter";
import { api } from "../../lib/api";
import { setLocale } from "../../lib/i18n";
import { deskSocketHub } from "../feed/deskSocketHub";
import { parseDeskMessage } from "../feed/messages";
import { AccountPanel, LedgerSections } from "./AccountPanel";
import {
  activePaperBarriersForInstrument,
  paperViewsToTradeExecutions,
} from "./usePaperAccounts";

function makeView(overrides: Partial<PaperAccountView> = {}): PaperAccountView {
  return {
    sub_account_id: "sa-momentum-very-long-subaccount-identifier-001",
    positions: [
      {
        round_id: "round-open-1",
        sub_account_id: "sa-momentum-very-long-subaccount-identifier-001",
        instrument_id: "BTC-USDT-PERP",
        direction: "LONG",
        quantity: "0.5",
        stop: "49500",
        take_profit: ["52000", "53500"],
        phase: "ACTIVE_PROTECTED",
        entry_price: "50000",
        mark_price: "50600",
        unrealized_pnl: "300",
        opened_ts: 1_700_000_060_000,
      },
    ],
    rounds: [
      {
        round_id: "round-open-1",
        sub_account_id: "sa-momentum-very-long-subaccount-identifier-001",
        instrument_id: "BTC-USDT-PERP",
        direction: "LONG",
        quantity: "0.5",
        stop: "49500",
        take_profit: ["52000", "53500"],
        phase: "ACTIVE_PROTECTED",
        entry_price: "50000",
        mark_price: "50600",
        unrealized_pnl: "300",
        opened_ts: 1_700_000_060_000,
      },
      {
        round_id: "round-pending-2",
        sub_account_id: "sa-momentum-very-long-subaccount-identifier-001",
        instrument_id: "BTC-USDT-PERP",
        direction: "SHORT",
        quantity: "0",
        stop: "51800",
        take_profit: ["48500"],
        phase: "PENDING_ENTRY",
        entry_low: "51000",
        entry_high: "51200",
      },
      {
        round_id: "round-closed-3",
        sub_account_id: "sa-momentum-very-long-subaccount-identifier-001",
        instrument_id: "BTC-USDT-PERP",
        direction: "LONG",
        quantity: "0",
        stop: "48000",
        take_profit: ["49500"],
        phase: "CLOSED",
        entry_price: "48500",
        opened_ts: 1_699_990_000_000,
        closed_ts: 1_699_995_000_000,
      },
    ],
    fills: [
      {
        event_id: "event-12",
        round_id: "round-open-1",
        instrument_id: "BTC-USDT-PERP",
        kind: "ENTRY_FILLED",
        ts_event: 1_700_000_065_000,
        quantity: "0.5",
        price: "50000",
        fee: "15",
        reason: "TAKER",
      },
      {
        event_id: "event-9",
        round_id: "round-closed-3",
        instrument_id: "BTC-USDT-PERP",
        kind: "EXIT_FILLED",
        ts_event: 1_699_995_000_000,
        quantity: "1",
        price: "49500",
        fee: "29.7",
        realized_pnl: "1000",
        reason: "TAKE_PROFIT",
      },
    ],
    ledger_seq: 12,
    account_state: {
      currency: "USDT",
      equity: "10300",
      cash_available: "9000",
      margin_used: "1000",
      trading_state: "ACTIVE",
      intraday_drawdown_pct: "0.0125",
      peak_drawdown_pct: "0.025",
      open_r: "1.5",
      risk_reserved: "250",
      margin_mode: "CROSS",
      leverage: "5",
    },
    feed_status: {
      healthy: true,
      updated_ts: 1_700_000_100_000,
    },
    process: {
      pid: 9876,
      scheme: "paper-breakout-v2",
      started_ts: 1_700_000_000_000,
      heartbeat_ts: 1_700_000_100_000,
      status: "RUNNING",
    },
    ...overrides,
  };
}

describe("Ticket 03: Paper AccountPanel, WS paper_account push, barriers & trade markers", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    setLocale("zh");
  });

  it("parses paper_account WS frame and maps paper fills & active barriers cleanly", () => {
    const view = makeView();
    const parsed = parseDeskMessage(JSON.stringify({ type: "paper_account", data: view }));
    expect(parsed).toEqual({ type: "paper_account", data: view });

    // Active barriers include OPEN + PENDING_ENTRY and exclude CLOSED.
    const activeRounds = activePaperBarriersForInstrument([view], "BTC-USDT-PERP");
    expect(activeRounds.map((r) => r.round_id)).toEqual(["round-open-1", "round-pending-2"]);

    const barriers = roundsToBarriers(activeRounds);
    expect(barriers[0]).toMatchObject({
      round_id: "round-open-1",
      entry_price: "50000",
      mark_price: "50600",
      unrealized_pnl: "300",
      stop: "49500",
    });
    expect(barriers[1]).toMatchObject({
      round_id: "round-pending-2",
      phase: "PENDING_ENTRY",
      entry_low: "51000",
      entry_high: "51200",
    });

    // Paper fills reuse roundsToTradeExecutions alignment.
    const tf5m = 300_000;
    const markers = paperViewsToTradeExecutions([view], tf5m, "BTC-USDT-PERP");
    expect(markers).toHaveLength(2);
    expect(markers[0]).toMatchObject({
      time: Math.floor(1_699_995_000_000 / tf5m) * tf5m,
      price: 49500,
      side: "sell",
      kind: "exit",
      label: "TP",
      tradeId: "round-closed-3",
    });
    expect(markers[1]).toMatchObject({
      time: Math.floor(1_700_000_065_000 / tf5m) * tf5m,
      price: 50000,
      side: "buy",
      kind: "entry",
      label: "ENTRY",
      tradeId: "round-open-1",
    });
  });

  it("renders balances, TradingState, positions, rounds, fills, and flips Online->Offline on WS push", async () => {
    const onlineView = makeView();
    const secondView = makeView({
      sub_account_id: "sa-empty-secondary",
      positions: [],
      rounds: [],
      fills: [],
      ledger_seq: 1,
      account_state: {
        currency: "USDT",
        equity: "5000",
        cash_available: "5000",
        margin_used: "0",
        trading_state: "REDUCING",
        trading_state_reason: "SUBSCRIPTION_LOST",
        intraday_drawdown_pct: "0",
        peak_drawdown_pct: "0",
        open_r: "0",
        risk_reserved: "0",
        margin_mode: "ISOLATED",
        leverage: "3",
      },
      process: {
        pid: 1111,
        scheme: "paper-alt",
        started_ts: 1_700_000_000_000,
        heartbeat_ts: 1_700_000_001_000,
        status: "OFFLINE",
        stopped_ts: 1_700_000_002_000,
      },
    });

    vi.spyOn(api, "paperAccounts").mockResolvedValue([
      onlineView.sub_account_id,
      secondView.sub_account_id,
    ]);
    vi.spyOn(api, "paperAccount").mockImplementation(async (id) =>
      id === onlineView.sub_account_id ? onlineView : secondView,
    );

    let pushedListener: ((view: PaperAccountView) => void) | null = null;
    vi.spyOn(deskSocketHub, "subscribePaperAccounts").mockImplementation((cb) => {
      pushedListener = cb;
      return () => {
        pushedListener = null;
      };
    });

    render(<AccountPanel />);

    // First sorted account is sa-empty-secondary; switch to long-name online account.
    await waitFor(() => {
      expect(screen.getByTestId("paper-account-select")).toBeTruthy();
    });
    const select = screen.getByTestId("paper-account-select") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: onlineView.sub_account_id } });

    expect(screen.getByTestId("paper-process-badge").textContent).toContain("在线");
    expect(screen.getByTestId("paper-equity").textContent).toContain("10300 USDT");
    expect(screen.getByTestId("paper-trading-state").textContent).toContain("ACTIVE");
    expect(screen.getByTestId("paper-positions-list")).toBeTruthy();
    expect(screen.getByTestId("paper-fills-list")).toBeTruthy();

    // Simulate WS push turning process OFFLINE (e.g. >5s stale heartbeat after kill).
    act(() => {
      pushedListener?.({
        ...onlineView,
        feed_status: { healthy: false, updated_ts: 1_700_000_100_000 },
        process: {
          ...onlineView.process!,
          status: "OFFLINE",
        },
      });
    });

    expect(screen.getByTestId("paper-process-badge").textContent).toContain("离线");
    expect(screen.getByTestId("paper-feed-warning")).toBeTruthy();

    // Switch to English and check empty positions state on secondary account.
    act(() => {
      setLocale("en");
    });
    fireEvent.change(select, { target: { value: secondView.sub_account_id } });
    expect(screen.getByTestId("paper-process-badge").textContent).toContain("Offline");
    expect(screen.getByTestId("paper-positions-empty").textContent).toContain("No open positions");
    expect(screen.getByTestId("paper-fills-empty").textContent).toContain("No fills yet");
  });

  it("renders empty accounts guidance when no projection exists", async () => {
    vi.spyOn(api, "paperAccounts").mockResolvedValue([]);
    vi.spyOn(deskSocketHub, "subscribePaperAccounts").mockReturnValue(() => undefined);

    render(<AccountPanel />);
    await waitFor(() => {
      expect(screen.getByTestId("paper-accounts-empty")).toBeTruthy();
    });
    expect(screen.getByTestId("paper-accounts-empty").textContent).toContain(
      "talaria account run",
    );
  });

  it("exports LedgerSections for direct reuse with legacy view without account_state", () => {
    setLocale("en");
    const legacy = makeView({ account_state: null, process: null, feed_status: null });
    render(<LedgerSections view={legacy} />);
    expect(screen.getByTestId("paper-positions-section")).toBeTruthy();
    expect(screen.getByTestId("paper-rounds-section")).toBeTruthy();
    expect(screen.getByTestId("paper-fills-section")).toBeTruthy();
  });

  it("Ticket 04: supports position intervention buttons, TradingState switcher, confirm modal, receipt display, and offline disabled state", async () => {
    const onlineView = makeView();
    vi.spyOn(api, "paperAccounts").mockResolvedValue([onlineView.sub_account_id]);
    vi.spyOn(api, "paperAccount").mockResolvedValue(onlineView);
    vi.spyOn(api, "paperCommands").mockResolvedValue([]);
    const submitSpy = vi.spyOn(api, "submitPaperCommand").mockImplementation(async (cmd) => ({
      command: cmd,
      claimed: true,
      receipt: {
        command_id: cmd.command_id,
        sub_account_id: cmd.sub_account_id,
        accepted: true,
        ledger_seq: 13,
        processed_ts: 1_700_000_200_000,
      },
    }));

    let pushedListener: ((view: PaperAccountView) => void) | null = null;
    vi.spyOn(deskSocketHub, "subscribePaperAccounts").mockImplementation((cb) => {
      pushedListener = cb;
      return () => {
        pushedListener = null;
      };
    });

    render(<AccountPanel />);
    await waitFor(() => {
      expect(screen.getByTestId("paper-btn-close-round-open-1")).toBeTruthy();
    });

    // Click Close All -> secondary confirmation dialog opens -> confirm submits CLOSE_ALL command.
    fireEvent.click(screen.getByTestId("paper-btn-close-round-open-1"));
    expect(screen.getByTestId("paper-confirm-dialog")).toBeTruthy();
    fireEvent.click(screen.getByTestId("paper-confirm-submit"));

    await waitFor(() => {
      expect(submitSpy).toHaveBeenCalledTimes(1);
    });
    expect(submitSpy.mock.calls[0][0]).toMatchObject({
      sub_account_id: onlineView.sub_account_id,
      round_id: "round-open-1",
      kind: "CLOSE_ALL",
      based_on_ledger_seq: 12,
    });
    expect(screen.getByTestId("paper-command-feedback").textContent).toContain("已执行");

    // Click REDUCING TradingState button -> confirm -> submits SET_TRADING_STATE.
    fireEvent.click(screen.getByTestId("paper-state-btn-REDUCING"));
    expect(screen.getByTestId("paper-confirm-dialog")).toBeTruthy();
    fireEvent.click(screen.getByTestId("paper-confirm-submit"));

    await waitFor(() => {
      expect(submitSpy).toHaveBeenCalledTimes(2);
    });
    expect(submitSpy.mock.calls[1][0]).toMatchObject({
      kind: "SET_TRADING_STATE",
      payload: { trading_state: "REDUCING" },
    });

    // Flip process OFFLINE via WS push -> all intervention & state buttons become disabled.
    act(() => {
      pushedListener?.({
        ...onlineView,
        process: { ...onlineView.process!, status: "OFFLINE" },
      });
    });
    expect((screen.getByTestId("paper-btn-close-round-open-1") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId("paper-btn-reduce-round-open-1") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId("paper-btn-breakeven-round-open-1") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId("paper-state-btn-REDUCING") as HTMLButtonElement).disabled).toBe(true);
  });
});
