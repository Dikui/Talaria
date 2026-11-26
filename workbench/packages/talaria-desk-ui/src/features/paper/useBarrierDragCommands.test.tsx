import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PaperAccountView } from "@talaria/contracts";

import { classifyBarrierDrag, type BarrierDragProposal } from "../../chart/barrierDragController";
import { api } from "../../lib/api";
import { setLocale } from "../../lib/i18n";
import { buildDraggableBarriers, useBarrierDragCommands } from "./useBarrierDragCommands";

function makeView(overrides: Partial<PaperAccountView> = {}): PaperAccountView {
  return {
    sub_account_id: "sa-alpha",
    positions: [
      {
        round_id: "round-1",
        sub_account_id: "sa-alpha",
        instrument_id: "BTC-USDT-PERP",
        direction: "LONG",
        quantity: "0.5",
        stop: "49000",
        take_profit: ["51500"],
        phase: "ACTIVE_PROTECTED",
        entry_price: "50000",
      },
    ],
    rounds: [
      {
        round_id: "round-1",
        sub_account_id: "sa-alpha",
        instrument_id: "BTC-USDT-PERP",
        direction: "LONG",
        quantity: "0.5",
        stop: "49000",
        take_profit: ["51500"],
        phase: "ACTIVE_PROTECTED",
        entry_price: "50000",
      },
    ],
    fills: [],
    ledger_seq: 18,
    account_state: {
      currency: "USDT",
      equity: "10000",
      cash_available: "9000",
      margin_used: "1000",
      trading_state: "ACTIVE",
      intraday_drawdown_pct: "0",
      peak_drawdown_pct: "0",
      open_r: "1",
      risk_reserved: "200",
      margin_mode: "CROSS",
      leverage: "5",
    },
    feed_status: { healthy: true, updated_ts: 1_700_000_000_000 },
    process: {
      pid: 1234,
      scheme: "paper-v1",
      started_ts: 1_700_000_000_000,
      heartbeat_ts: 1_700_000_000_000,
      status: "RUNNING",
    },
    ...overrides,
  };
}

function makeProposal(targetPrice: string): BarrierDragProposal {
  const barrier = {
    round_id: "round-1",
    sub_account_id: "sa-alpha",
    direction: "LONG" as const,
    stop: "49000",
    take_profit: ["51500"],
    entry_price: "50000",
    quantity: "0.5",
    ledger_seq: 18,
  };
  return {
    roundId: "round-1",
    subAccountId: "sa-alpha",
    direction: "LONG",
    kind: "STOP",
    tpIndex: -1,
    originalPrice: "49000",
    targetPrice,
    entryPrice: "50000",
    basedOnLedgerSeq: 18,
    classification: classifyBarrierDrag(barrier, "STOP", -1, targetPrice),
    payload: { stop: targetPrice },
  };
}

describe("Ticket 08: useBarrierDragCommands", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    setLocale("en");
  });

  it("enriches barriers with offline / replay / HALTED disabled metadata", () => {
    const running = makeView();
    const offline = makeView({
      sub_account_id: "sa-off",
      process: { ...running.process!, status: "OFFLINE" },
    });
    const halted = makeView({
      sub_account_id: "sa-halt",
      account_state: { ...running.account_state!, trading_state: "HALTED" },
    });

    const enriched = buildDraggableBarriers(
      [
        running.rounds[0],
        { ...running.rounds[0], sub_account_id: "sa-off", round_id: "r-off" },
        { ...running.rounds[0], sub_account_id: "sa-halt", round_id: "r-halt" },
      ],
      { "sa-alpha": running, "sa-off": offline, "sa-halt": halted },
    );

    expect(enriched[0]).toMatchObject({ draggable: true, disabled_reason: null, ledger_seq: 18 });
    expect(enriched[1]).toMatchObject({ draggable: false, disabled_reason: "OFFLINE" });
    expect(enriched[2]).toMatchObject({ draggable: false, disabled_reason: "HALTED" });

    const replayEnriched = buildDraggableBarriers([running.rounds[0]], { "sa-alpha": running }, { replayActive: true });
    expect(replayEnriched[0]).toMatchObject({ draggable: false, disabled_reason: "REPLAY" });
  });

  it("cancels modal with zero command, resolves tighten on WS update, reverts widen rejection and COMMAND_STALE", async () => {
    const onCancelPendingConfirm = vi.fn();
    const onMarkPendingAck = vi.fn();
    const onResolvePendingAck = vi.fn();

    const submitSpy = vi.spyOn(api, "submitPaperCommand");
    const listSpy = vi.spyOn(api, "paperCommands").mockResolvedValue([]);

    const initialView = makeView();
    const { result, rerender } = renderHook(
      ({ viewsById }) =>
        useBarrierDragCommands({
          viewsById,
          onCancelPendingConfirm,
          onMarkPendingAck,
          onResolvePendingAck,
          pollIntervalMs: 50,
        }),
      { initialProps: { viewsById: { "sa-alpha": initialView } } },
    );

    // 1. Cancel after drag -> zero command sent.
    const tightenProposal = makeProposal("49600");
    act(() => result.current.handleDragEnd(tightenProposal));
    expect(result.current.pendingProposal).toEqual(tightenProposal);
    act(() => result.current.handleCancelModal());
    expect(result.current.pendingProposal).toBeNull();
    expect(onCancelPendingConfirm).toHaveBeenCalledTimes(1);
    expect(submitSpy).not.toHaveBeenCalled();

    // 2. Confirm widen rejected by risk gate (MAX_SINGLE_R_EXCEEDED) -> reverts & displays reason.
    const widenProposal = makeProposal("47000");
    submitSpy.mockImplementationOnce(async (cmd) => ({
      command: cmd,
      claimed: false,
    }));
    listSpy.mockResolvedValueOnce([
      {
        command: {
          command_id: "will-be-replaced",
          sub_account_id: "sa-alpha",
          kind: "WIDEN_STOP",
          payload: { stop: "47000" },
          based_on_ledger_seq: 18,
          issued_by: "trader",
          issued_ts: 1,
        },
        claimed: true,
      },
    ]);

    let submittedCmdId = "";
    submitSpy.mockReset();
    submitSpy.mockImplementationOnce(async (cmd) => {
      submittedCmdId = cmd.command_id;
      return { command: cmd, claimed: false };
    });
    listSpy.mockImplementation(async () => [
      {
        command: {
          command_id: submittedCmdId,
          sub_account_id: "sa-alpha",
          round_id: "round-1",
          kind: "WIDEN_STOP",
          payload: { stop: "47000" },
          based_on_ledger_seq: 18,
          issued_by: "trader",
          issued_ts: 1,
        },
        claimed: true,
        receipt: {
          command_id: submittedCmdId,
          sub_account_id: "sa-alpha",
          accepted: false,
          reason: "MAX_SINGLE_R_EXCEEDED",
          ledger_seq: 18,
          processed_ts: 2,
        },
      },
    ]);

    await act(async () => {
      await result.current.handleConfirmModal(widenProposal, "wider stop");
    });
    expect(onMarkPendingAck).toHaveBeenCalledWith(submittedCmdId, widenProposal);

    await waitFor(() => {
      expect(result.current.notice?.tone).toBe("error");
    });
    expect(result.current.notice?.text).toContain("MAX_SINGLE_R_EXCEEDED");
    expect(onResolvePendingAck).toHaveBeenCalledWith(submittedCmdId);

    // 3. Barrier triggered during drag -> COMMAND_STALE converges cleanly when round closes.
    let staleCmdId = "";
    submitSpy.mockImplementationOnce(async (cmd) => {
      staleCmdId = cmd.command_id;
      return { command: cmd, claimed: false };
    });
    listSpy.mockResolvedValue([]);

    await act(async () => {
      await result.current.handleConfirmModal(tightenProposal);
    });

    // Simulate WS push where round-1 already closed on stop-loss fill!
    listSpy.mockResolvedValueOnce([
      {
        command: {
          command_id: staleCmdId,
          sub_account_id: "sa-alpha",
          round_id: "round-1",
          kind: "TIGHTEN_STOP",
          payload: { stop: "49600" },
          based_on_ledger_seq: 18,
          issued_by: "trader",
          issued_ts: 3,
        },
        claimed: true,
        receipt: {
          command_id: staleCmdId,
          sub_account_id: "sa-alpha",
          accepted: false,
          reason: "COMMAND_STALE",
          ledger_seq: 19,
          processed_ts: 4,
        },
      },
    ]);

    const closedView = makeView({
      ledger_seq: 19,
      positions: [],
      rounds: [{ ...initialView.rounds[0], phase: "CLOSED", quantity: "0" }],
    });
    rerender({ viewsById: { "sa-alpha": closedView } });

    await waitFor(() => {
      expect(result.current.notice?.text).toContain("COMMAND_STALE");
    });
    expect(onResolvePendingAck).toHaveBeenCalledWith(staleCmdId);

    // 4. Happy-path tighten resolves immediately when WS projection updates stop price.
    rerender({ viewsById: { "sa-alpha": initialView } });
    let okCmdId = "";
    submitSpy.mockImplementationOnce(async (cmd) => {
      okCmdId = cmd.command_id;
      return { command: cmd, claimed: false };
    });
    listSpy.mockResolvedValue([]);

    await act(async () => {
      await result.current.handleConfirmModal(tightenProposal);
    });
    expect(result.current.pendingAck?.commandId).toBe(okCmdId);

    const tightenedView = makeView({
      ledger_seq: 20,
      positions: [{ ...initialView.positions[0], stop: "49600" }],
      rounds: [{ ...initialView.rounds[0], stop: "49600" }],
    });
    rerender({ viewsById: { "sa-alpha": tightenedView } });

    await waitFor(() => {
      expect(result.current.notice?.tone).toBe("ok");
    });
    expect(result.current.notice?.text).toContain("49600");
    expect(onResolvePendingAck).toHaveBeenCalledWith(okCmdId);

    // 5. Disabled attempt & 10s timeout notices
    act(() => {
      result.current.handleDisabledAttempt({
        barrier: {
          round_id: "round-1",
          sub_account_id: "sa-alpha",
          direction: "LONG",
          stop: "49000",
          take_profit: ["51500"],
        },
        kind: "STOP",
        reason: "OFFLINE",
        clientX: 10,
        clientY: 10,
      });
    });
    expect(result.current.notice?.text).toContain("offline");
  });
});
