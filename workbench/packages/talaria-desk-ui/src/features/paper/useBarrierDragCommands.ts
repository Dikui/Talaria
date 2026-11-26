import { useCallback, useEffect, useRef, useState } from "react";
import type { PaperAccountView, PaperCommandReceipt, PaperRound } from "@talaria/contracts";

import {
  disabledReasonLabel,
  type BarrierDragDisabledEvent,
  type BarrierDragDisabledReason,
  type BarrierDragOverlay,
  type BarrierDragProposal,
  type DraggableBarrierSpec,
} from "../../chart/barrierDragController";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import { isPaperProcessOnline } from "./usePaperAccounts";

export interface BarrierDragNotice {
  tone: "ok" | "warn" | "error";
  text: string;
}

export interface UseBarrierDragCommandsOptions {
  viewsById: Record<string, PaperAccountView>;
  onCancelPendingConfirm: () => void;
  onMarkPendingAck: (commandId: string, proposal: BarrierDragProposal) => void;
  onResolvePendingAck: (commandId?: string) => void;
  pollIntervalMs?: number;
}

const TERMINAL_PHASES = new Set(["CLOSED", "CANCELLED", "LIQUIDATED"]);

export function buildDraggableBarriers(
  rounds: readonly PaperRound[],
  viewsById: Record<string, PaperAccountView>,
  opts?: { readOnly?: boolean; replayActive?: boolean },
): DraggableBarrierSpec[] {
  return rounds.map((round) => {
    const view = viewsById[round.sub_account_id];
    const tradingState = view?.account_state?.trading_state ?? null;
    let disabledReason: BarrierDragDisabledReason | null = null;
    if (opts?.replayActive) {
      disabledReason = "REPLAY";
    } else if (round.phase && TERMINAL_PHASES.has(round.phase)) {
      disabledReason = "NOT_ACTIVE_ROUND";
    } else if (!isPaperProcessOnline(view)) {
      disabledReason = "OFFLINE";
    } else if (tradingState === "HALTED") {
      disabledReason = "HALTED";
    } else if (opts?.readOnly) {
      disabledReason = "READ_ONLY";
    }

    return {
      round_id: round.round_id,
      sub_account_id: round.sub_account_id,
      direction: round.direction,
      stop: round.stop,
      take_profit: Array.isArray(round.take_profit) ? round.take_profit : [],
      phase: round.phase ?? null,
      entry_low: round.entry_low ?? null,
      entry_high: round.entry_high ?? null,
      entry_price: round.entry_price ?? null,
      mark_price: round.mark_price ?? null,
      unrealized_pnl: round.unrealized_pnl ?? null,
      quantity: round.quantity ?? null,
      ledger_seq: view?.ledger_seq ?? 0,
      draggable: disabledReason === null,
      disabled_reason: disabledReason,
      account_trading_state: tradingState,
    };
  });
}

function formatReceiptNotice(
  proposal: BarrierDragProposal,
  receipt: PaperCommandReceipt,
): BarrierDragNotice {
  const tag =
    proposal.kind === "STOP"
      ? `SL ${proposal.targetPrice}`
      : `TP${proposal.tpIndex + 1} ${proposal.targetPrice}`;
  if (receipt.accepted) {
    if (receipt.reason === "NOOP") {
      return {
        tone: "ok",
        text: t(`${tag} 无变化 (NOOP)`, `${tag} unchanged (NOOP)`),
      };
    }
    return {
      tone: "ok",
      text: t(`改单已生效：${tag}`, `Order updated: ${tag}`),
    };
  }
  if (receipt.reason === "COMMAND_STALE") {
    return {
      tone: "warn",
      text: t(
        `改单已过期 (COMMAND_STALE)：持仓或屏障状态已变化，线条已同步至最新账本`,
        `Order stale (COMMAND_STALE): position or barrier already changed — reverted to latest ledger`,
      ),
    };
  }
  const code = receipt.reason || t("未知原因", "unknown");
  return {
    tone: "error",
    text: t(
      `改单被拒绝 (${code})，线条已恢复至原价 ${proposal.originalPrice}`,
      `Order rejected (${code}) — reverted to ${proposal.originalPrice}`,
    ),
  };
}

function barrierMatchesProposal(
  view: PaperAccountView,
  proposal: BarrierDragProposal,
): boolean {
  const candidates = [...(view.positions ?? []), ...(view.rounds ?? [])];
  const round = candidates.find((r) => r.round_id === proposal.roundId);
  if (!round) return false;
  if (proposal.kind === "STOP") {
    return Number(round.stop) === Number(proposal.targetPrice);
  }
  const targetTp = round.take_profit?.[proposal.tpIndex];
  return targetTp !== undefined && Number(targetTp) === Number(proposal.targetPrice);
}

export function useBarrierDragCommands({
  viewsById,
  onCancelPendingConfirm,
  onMarkPendingAck,
  onResolvePendingAck,
  pollIntervalMs = 350,
}: UseBarrierDragCommandsOptions) {
  const [pendingProposal, setPendingProposal] = useState<BarrierDragProposal | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [pendingAck, setPendingAck] = useState<{
    commandId: string;
    proposal: BarrierDragProposal;
  } | null>(null);
  const [notice, setNotice] = useState<BarrierDragNotice | null>(null);

  const pendingAckRef = useRef(pendingAck);
  pendingAckRef.current = pendingAck;

  const handleDragEnd = useCallback((proposal: BarrierDragProposal) => {
    setPendingProposal(proposal);
  }, []);

  const handleCancelModal = useCallback(() => {
    setPendingProposal(null);
    onCancelPendingConfirm();
  }, [onCancelPendingConfirm]);

  const finishPendingAck = useCallback(
    (commandId: string, nextNotice: BarrierDragNotice) => {
      if (pendingAckRef.current?.commandId === commandId) {
        pendingAckRef.current = null;
        setPendingAck(null);
      }
      onResolvePendingAck(commandId);
      setNotice(nextNotice);
    },
    [onResolvePendingAck],
  );

  const handleConfirmModal = useCallback(
    async (proposal: BarrierDragProposal, reason?: string) => {
      setSubmitting(true);
      const commandId = `cmd-drag-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const payload = reason ? { ...proposal.payload, reason } : proposal.payload;
      try {
        const record = await api.submitPaperCommand({
          command_id: commandId,
          sub_account_id: proposal.subAccountId,
          round_id: proposal.roundId,
          kind: proposal.classification.commandKind,
          payload,
          based_on_ledger_seq: proposal.basedOnLedgerSeq,
          issued_by: "trader",
          issued_ts: Date.now(),
        });
        setPendingProposal(null);
        if (record.receipt) {
          onResolvePendingAck(commandId);
          setNotice(formatReceiptNotice(proposal, record.receipt));
        } else {
          const next = { commandId, proposal };
          pendingAckRef.current = next;
          setPendingAck(next);
          onMarkPendingAck(commandId, proposal);
          setNotice({
            tone: "warn",
            text: t("改单待执行…", "Order modification pending…"),
          });
        }
      } catch (caught) {
        setPendingProposal(null);
        onCancelPendingConfirm();
        const detail = caught instanceof Error ? caught.message : String(caught);
        setNotice({
          tone: "error",
          text: t(
            `改单提交失败：${detail}，线条已恢复至原价 ${proposal.originalPrice}`,
            `Failed to submit command: ${detail} — reverted to ${proposal.originalPrice}`,
          ),
        });
      } finally {
        setSubmitting(false);
      }
    },
    [onCancelPendingConfirm, onMarkPendingAck, onResolvePendingAck],
  );

  const handlePendingAckTimeout = useCallback((overlay: BarrierDragOverlay) => {
    if (
      overlay.commandId &&
      pendingAckRef.current?.commandId === overlay.commandId
    ) {
      pendingAckRef.current = null;
      setPendingAck(null);
    }
    setNotice({
      tone: "warn",
      text: t(
        `10 秒内未收到账户回执，线条已恢复至原价 ${overlay.originalPrice}`,
        `No receipt within 10s — reverted to ${overlay.originalPrice}`,
      ),
    });
  }, []);

  const handleDisabledAttempt = useCallback((ev: BarrierDragDisabledEvent) => {
    setNotice({
      tone: "warn",
      text: disabledReasonLabel(ev.reason),
    });
  }, []);

  // Poll command receipts while pending_ack is active so rejections / COMMAND_STALE
  // converge immediately even when ledger_seq does not change.
  useEffect(() => {
    if (!pendingAck) return;
    let cancelled = false;
    const { commandId, proposal } = pendingAck;

    const pollOnce = async () => {
      try {
        const records = await api.paperCommands(proposal.subAccountId);
        if (cancelled || pendingAckRef.current?.commandId !== commandId) return;
        const found = records.find((r) => r.command.command_id === commandId);
        if (found?.receipt) {
          finishPendingAck(commandId, formatReceiptNotice(proposal, found.receipt));
        }
      } catch {
        // Retry on next interval until timeout handles rollback.
      }
    };

    const timer = setInterval(() => {
      void pollOnce();
    }, pollIntervalMs);
    void pollOnce();

    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [pendingAck, pollIntervalMs, finishPendingAck]);

  // Also converge immediately when WS paper_account projection advances past based_on_ledger_seq.
  useEffect(() => {
    if (!pendingAck) return;
    const { commandId, proposal } = pendingAck;
    const view = viewsById[proposal.subAccountId];
    if (!view || (view.ledger_seq ?? 0) <= proposal.basedOnLedgerSeq) return;

    if (barrierMatchesProposal(view, proposal)) {
      finishPendingAck(commandId, {
        tone: "ok",
        text: t(
          `改单已生效：${proposal.kind === "STOP" ? "SL" : `TP${proposal.tpIndex + 1}`} ${proposal.targetPrice}`,
          `Order updated: ${proposal.kind === "STOP" ? "SL" : `TP${proposal.tpIndex + 1}`} ${proposal.targetPrice}`,
        ),
      });
      return;
    }

    // If round closed/disappeared while pending, check receipt once to surface COMMAND_STALE cleanly.
    const round = [...(view.rounds ?? []), ...(view.positions ?? [])].find(
      (r) => r.round_id === proposal.roundId,
    );
    if (!round || (round.phase && TERMINAL_PHASES.has(round.phase))) {
      void api
        .paperCommands(proposal.subAccountId)
        .then((records) => {
          if (pendingAckRef.current?.commandId !== commandId) return;
          const found = records.find((r) => r.command.command_id === commandId);
          if (found?.receipt) {
            finishPendingAck(commandId, formatReceiptNotice(proposal, found.receipt));
          } else {
            finishPendingAck(commandId, {
              tone: "warn",
              text: t(
                "回合已触发平仓 (COMMAND_STALE)，线条已同步至最新账本",
                "Round already closed (COMMAND_STALE) — synced with latest ledger",
              ),
            });
          }
        })
        .catch(() => undefined);
    }
  }, [viewsById, pendingAck, finishPendingAck]);

  const dismissNotice = useCallback(() => setNotice(null), []);

  return {
    pendingProposal,
    submitting,
    pendingAck,
    notice,
    handleDragEnd,
    handleCancelModal,
    handleConfirmModal,
    handlePendingAckTimeout,
    handleDisabledAttempt,
    dismissNotice,
  };
}
