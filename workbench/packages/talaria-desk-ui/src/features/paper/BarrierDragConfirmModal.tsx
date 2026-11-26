import { useEffect, useState } from "react";

import type { BarrierDragProposal } from "../../chart/barrierDragController";
import { t, useLocale } from "../../lib/i18n";
import "./BarrierDragConfirmModal.css";

export interface BarrierDragConfirmModalProps {
  proposal: BarrierDragProposal | null;
  submitting?: boolean;
  onCancel: () => void;
  onConfirm: (proposal: BarrierDragProposal, reason?: string) => void;
}

function commandTitle(proposal: BarrierDragProposal): string {
  if (proposal.kind === "STOP") {
    return proposal.classification.isWidening
      ? t("确认放宽止损", "Confirm widen stop loss")
      : t("确认收紧止损", "Confirm tighten stop loss");
  }
  const tpLabel = proposal.tpIndex >= 0 ? `TP${proposal.tpIndex + 1}` : "TP";
  return proposal.classification.isWidening
    ? t(`确认放宽止盈 (${tpLabel})`, `Confirm widen take profit (${tpLabel})`)
    : t(`确认收紧止盈 (${tpLabel})`, `Confirm tighten take profit (${tpLabel})`);
}

function formatDeltaR(rDelta: number | null, kind: "STOP" | "TP"): string | null {
  if (rDelta === null || !Number.isFinite(rDelta)) return null;
  const sign = rDelta > 0 ? "+" : "";
  const formatted = `${sign}${rDelta.toFixed(2)} R`;
  return kind === "STOP"
    ? t(`风险变化 ${formatted}`, `Risk delta ${formatted}`)
    : t(`目标收益变化 ${formatted}`, `Reward delta ${formatted}`);
}

/**
 * Spec D10 secondary confirmation modal shown on barrier drag pointerup.
 * Cancel immediately reverts the line with zero command submitted; Confirm
 * submits the classified command with `based_on_ledger_seq`.
 */
export function BarrierDragConfirmModal({
  proposal,
  submitting = false,
  onCancel,
  onConfirm,
}: BarrierDragConfirmModalProps) {
  useLocale();
  const [reason, setReason] = useState("");

  useEffect(() => {
    setReason("");
  }, [proposal]);

  useEffect(() => {
    if (!proposal) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !submitting) {
        onCancel();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [proposal, submitting, onCancel]);

  if (!proposal) return null;

  const { classification } = proposal;
  const deltaRText = formatDeltaR(classification.rDelta, proposal.kind);

  return (
    <div
      className="barrier-confirm-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label={commandTitle(proposal)}
      data-testid="barrier-drag-confirm-dialog"
    >
      <div className="barrier-confirm-card">
        <div className="barrier-confirm-head">
          <h3>{commandTitle(proposal)}</h3>
          <span className="barrier-confirm-seq">
            {t(`账本 #${proposal.basedOnLedgerSeq}`, `Ledger #${proposal.basedOnLedgerSeq}`)}
          </span>
        </div>

        <p className="barrier-confirm-meta">
          <strong>{proposal.subAccountId}</strong> · {proposal.roundId} ({proposal.direction})
        </p>

        <div className="barrier-confirm-price-row" data-testid="barrier-drag-price-diff">
          <span className="barrier-confirm-old">{proposal.originalPrice}</span>
          <span className="barrier-confirm-arrow" aria-hidden="true">
            →
          </span>
          <span className="barrier-confirm-new">{proposal.targetPrice}</span>
        </div>

        <div className="barrier-confirm-badges">
          {deltaRText && (
            <span
              className={`barrier-confirm-pill ${
                classification.isWidening
                  ? "barrier-confirm-pill--warn"
                  : "barrier-confirm-pill--ok"
              }`}
              data-testid="barrier-drag-r-delta"
            >
              {deltaRText}
            </span>
          )}
          {classification.crossesBreakeven && (
            <span
              className="barrier-confirm-pill barrier-confirm-pill--be"
              data-testid="barrier-drag-breakeven-badge"
            >
              {t("已越过入场价 · 保本", "Crosses entry · Breakeven")}
            </span>
          )}
          {classification.requiresRiskCheck ? (
            <span
              className="barrier-confirm-pill barrier-confirm-pill--warn"
              data-testid="barrier-drag-risk-badge"
            >
              {t("需风控审批 (ADR-0015)", "Requires risk approval (ADR-0015)")}
            </span>
          ) : (
            <span
              className="barrier-confirm-pill barrier-confirm-pill--ok"
              data-testid="barrier-drag-direct-badge"
            >
              {t("只减风险 · 直达成交通道", "Risk-reducing · Direct execution")}
            </span>
          )}
        </div>

        <label className="barrier-confirm-reason">
          <span>{t("备注原因（可选）", "Reason note (optional)")}</span>
          <input
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder={t("例如：跟随结构抬高止损", "e.g. trail stop above structure")}
            disabled={submitting}
            data-testid="barrier-drag-reason-input"
          />
        </label>

        <div className="barrier-confirm-actions">
          <button
            type="button"
            className="barrier-confirm-btn"
            disabled={submitting}
            onClick={onCancel}
            data-testid="barrier-drag-cancel"
          >
            {t("取消并归位", "Cancel & revert")}
          </button>
          <button
            type="button"
            className={`barrier-confirm-btn ${
              classification.requiresRiskCheck
                ? "barrier-confirm-btn--warn"
                : "barrier-confirm-btn--primary"
            }`}
            disabled={submitting}
            onClick={() => onConfirm(proposal, reason.trim() || undefined)}
            data-testid="barrier-drag-submit"
          >
            {submitting
              ? t("提交中…", "Submitting…")
              : t("确认改单", "Confirm modify")}
          </button>
        </div>
      </div>
    </div>
  );
}
