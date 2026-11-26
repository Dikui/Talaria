import { useEffect, useId, useMemo, useRef, useState, type FormEvent } from "react";
import type { DraftRecord } from "@talaria/contracts";

import { api } from "../../lib/api";
import { getLocale, t, useLocale } from "../../lib/i18n";
import { DraftForm, newManualDraft, type DraftSeed } from "./DraftForm";
import { usePaperQuery } from "./usePaperQuery";
import "./paper.css";

function draftStatusSummary(draft: DraftRecord): {
  label: string;
  tone: "accepted" | "rejected" | "muted" | "neutral";
} {
  const receipt = draft.receipt;
  if (receipt?.accepted) {
    return {
      label: t("已执行 · 通过定仓与风控", "Executed · passed sizing & risk"),
      tone: "accepted",
    };
  }
  if (receipt) {
    if (receipt.reason === "DRAFT_STALE") {
      return {
        label: t("草案已过期 · DRAFT_STALE", "Draft stale · DRAFT_STALE"),
        tone: "rejected",
      };
    }
    return {
      label: t(
        `账户拒绝 · ${receipt.reason ?? "未知原因"}`,
        `Account rejected · ${receipt.reason ?? "unknown reason"}`,
      ),
      tone: "rejected",
    };
  }
  if (draft.status === "DISCARDED") {
    return { label: t("已丢弃", "Discarded"), tone: "muted" };
  }
  if (draft.status === "EXPIRED") {
    return { label: t("已过期", "Expired"), tone: "muted" };
  }
  if (draft.claimed) {
    return { label: t("账户处理中", "Account processing"), tone: "neutral" };
  }
  if (draft.status === "CONFIRMED") {
    return {
      label: t("已确认 · 等待账户接收", "Confirmed · awaiting account"),
      tone: "neutral",
    };
  }
  return { label: t("待确认", "Pending confirmation"), tone: "neutral" };
}

export function DraftReview({
  draft,
  readOnly = false,
  highlighted = false,
  knownAccounts = [],
  previewedQuantity = null,
  onPreviewQuantityRecorded,
  refresh,
}: {
  draft: DraftRecord;
  readOnly?: boolean;
  highlighted?: boolean;
  knownAccounts?: readonly string[];
  previewedQuantity?: string | null;
  onPreviewQuantityRecorded?: (draftId: string, qty: string | null) => void;
  refresh: () => void;
}) {
  useLocale();
  const cardRef = useRef<HTMLElement | null>(null);
  const [editing, setEditing] = useState<DraftRecord>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const problem = useId();

  useEffect(() => {
    if (highlighted) {
      cardRef.current?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
    }
  }, [highlighted]);

  const isTerminal =
    draft.status === "DISCARDED" || draft.status === "EXPIRED" || Boolean(draft.receipt);
  const canMutate = !readOnly && !draft.claimed && !isTerminal;

  if (editing && canMutate) {
    return (
      <DraftForm
        seed={editing.content}
        draft={editing}
        knownAccounts={knownAccounts}
        onClose={() => setEditing(undefined)}
        onSaved={(saved, qty) => {
          onPreviewQuantityRecorded?.(saved.draft_id, qty ?? null);
          setEditing(undefined);
          refresh();
        }}
      />
    );
  }

  async function confirm(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const field = event.currentTarget.elements.namedItem(
      "confirmed_by",
    ) as HTMLInputElement | null;
    const reviewed = new FormData(event.currentTarget);
    const confirmedBy = (field?.value ?? "").trim();
    if (!confirmedBy) {
      setError(t("请输入确认人。", "Enter a confirmer."));
      field?.focus();
      return;
    }
    setBusy(true);
    setError(undefined);
    try {
      await api.confirmPaperDraft(draft.draft_id, {
        version: Number(reviewed.get("version")),
        content_hash: String(reviewed.get("content_hash")),
        confirmed_by: confirmedBy,
      });
      refresh();
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : t("确认失败，请重试。", "Confirmation failed. Please try again."),
      );
      refresh();
    } finally {
      setBusy(false);
    }
  }

  async function discard() {
    if (busy) return;
    setBusy(true);
    setError(undefined);
    try {
      await api.discardPaperDraft(draft.draft_id, { version: draft.version });
      refresh();
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : t("丢弃失败，请重试。", "Discard failed. Please try again."),
      );
      refresh();
    } finally {
      setBusy(false);
    }
  }

  const content = draft.content;
  const receipt = draft.receipt;
  const summary = draftStatusSummary(draft);
  const badgeClass =
    summary.tone === "accepted"
      ? "paper-status-badge is-accepted"
      : summary.tone === "rejected"
        ? "paper-status-badge is-rejected"
        : summary.tone === "muted"
          ? "paper-status-badge is-muted"
          : "paper-status-badge";

  return (
    <article
      ref={cardRef}
      className={`paper-draft-review${highlighted ? " is-highlighted" : ""}`}
      data-testid={`draft-card-${draft.draft_id}`}
      data-highlighted={highlighted ? "true" : undefined}
    >
      <header>
        <strong className="paper-instrument">{content.instrument_id}</strong>
        <span className={badgeClass}>{summary.label}</span>
      </header>
      <p className="paper-muted">
        {content.sub_account_id} · {t("版本", "Version")} {draft.version} ·{" "}
        {content.consultation_id ? t("协同会诊", "Consultation") : t("人工", "Manual")} ·{" "}
        {content.direction === "LONG" ? t("做多", "Long") : t("做空", "Short")}
      </p>
      <dl>
        {receipt?.plan_id && (
          <div>
            <dt>{t("执行计划", "Execution plan")}</dt>
            <dd>{receipt.plan_id}</dd>
          </div>
        )}
        {receipt?.quantity && (
          <div>
            <dt>{t("实际定仓数量", "Actual sized qty")}</dt>
            <dd>
              {receipt.quantity}
              {previewedQuantity && previewedQuantity !== receipt.quantity && (
                <span className="paper-sizing-diff" data-testid="paper-sizing-qty-diff">
                  {" "}
                  ({t(`预演 ${previewedQuantity}`, `preview ${previewedQuantity}`)})
                </span>
              )}
            </dd>
          </div>
        )}
        {content.inherited_barriers && (
          <>
            <div>
              <dt>{t("加仓回合", "Scaling round")}</dt>
              <dd>{content.round_id}</dd>
            </div>
            <div>
              <dt>{t("最长持仓", "Max holding")}</dt>
              <dd>
                {content.inherited_barriers.max_holding_ms / 1000} {t("秒", "s")}
              </dd>
            </div>
            <div>
              <dt>{t("移动止损", "Trailing stop")}</dt>
              <dd>
                {content.inherited_barriers.trailing_stop
                  ? t(
                      `激活幅度 ${content.inherited_barriers.trailing_stop.activation_pct}，回撤幅度 ${content.inherited_barriers.trailing_stop.distance_pct}`,
                      `activation ${content.inherited_barriers.trailing_stop.activation_pct}, distance ${content.inherited_barriers.trailing_stop.distance_pct}`,
                    )
                  : t("未启用", "Disabled")}
              </dd>
            </div>
          </>
        )}
        <div>
          <dt>{t("证据快照", "Evidence snapshot")}</dt>
          <dd>{content.snapshot_id}</dd>
        </div>
        <div>
          <dt>{t("有效期", "Valid until")}</dt>
          <dd>
            {content.valid_until == null ? (
              t("未设截止时刻", "No deadline")
            ) : (
              <time dateTime={new Date(content.valid_until).toISOString()}>
                {new Date(content.valid_until).toLocaleString(
                  getLocale() === "zh" ? "zh-CN" : "en-US",
                )}
              </time>
            )}
          </dd>
        </div>
        <div>
          <dt>{t("入场价带", "Entry band")}</dt>
          <dd>
            {content.entry_low}–{content.entry_high}
          </dd>
        </div>
        <div>
          <dt>{t("止损", "Stop loss")}</dt>
          <dd>{content.invalidation_stop}</dd>
        </div>
        <div>
          <dt>{t("止盈", "Take profit")}</dt>
          <dd>{content.take_profit.join(" / ")}</dd>
        </div>
      </dl>
      {canMutate && (
        <form className="paper-actions" noValidate onSubmit={confirm} aria-busy={busy}>
          {draft.status !== "CONFIRMED" && (
            <div className="paper-confirm-row">
              <input type="hidden" name="version" value={draft.version} />
              <input type="hidden" name="content_hash" value={draft.content_hash} />
              <label>
                {t("确认人", "Confirmer")}
                <input
                  name="confirmed_by"
                  maxLength={80}
                  defaultValue="trader"
                  required
                  aria-describedby={error ? problem : undefined}
                />
              </label>
              <button type="submit" className="primary-button" disabled={busy}>
                {busy
                  ? t("正在确认…", "Confirming…")
                  : t("确认并提交模拟盘", "Confirm & submit")}
              </button>
            </div>
          )}
          <div className="paper-actions-row">
            <button
              type="button"
              className="ghost-button"
              disabled={busy}
              onClick={() => setEditing(draft)}
            >
              {t("编辑草案", "Edit draft")}
            </button>
            <button
              type="button"
              className="ghost-button"
              disabled={busy}
              onClick={() => void discard()}
            >
              {t("丢弃草案", "Discard draft")}
            </button>
          </div>
        </form>
      )}
      {error && (
        <p id={problem} className="paper-error" role="alert">
          {error}
        </p>
      )}
    </article>
  );
}

export interface DraftPanelProps {
  activeInstrumentId: string;
  activeTimeframe: string;
  readOnly?: boolean;
  replayActive?: boolean;
  pollIntervalMs?: number;
  externalSeed?: (DraftSeed & { nonce: number }) | null;
  onExternalSeedConsumed?: (nonce: number) => void;
  highlightedDraftId?: string | null;
  onDraftsMutated?: () => void;
}

export function DraftPanel({
  activeInstrumentId,
  activeTimeframe,
  readOnly = false,
  replayActive = false,
  pollIntervalMs = 5000,
  externalSeed = null,
  onExternalSeedConsumed,
  highlightedDraftId = null,
  onDraftsMutated,
}: DraftPanelProps) {
  useLocale();
  const drafts = usePaperQuery(api.paperDrafts, pollIntervalMs);
  const [creatingSeed, setCreatingSeed] = useState<(DraftSeed & { nonce?: number }) | null>(null);
  const [creatingBusy, setCreatingBusy] = useState(false);
  const [createError, setCreateError] = useState<string>();
  const [previewedQtyByDraft, setPreviewedQtyByDraft] = useState<Record<string, string>>({});
  const consumedNonceRef = useRef<number | null>(null);

  useEffect(() => {
    if (!externalSeed || readOnly || replayActive) return;
    if (consumedNonceRef.current === externalSeed.nonce) return;
    consumedNonceRef.current = externalSeed.nonce;
    setCreatingSeed(externalSeed);
    setCreateError(undefined);
    onExternalSeedConsumed?.(externalSeed.nonce);
  }, [externalSeed, readOnly, replayActive, onExternalSeedConsumed]);

  const refreshAndNotify = () => {
    drafts.refresh();
    onDraftsMutated?.();
  };

  const recordPreviewQty = (draftId: string, qty: string | null) => {
    if (!qty) return;
    setPreviewedQtyByDraft((prev) => ({ ...prev, [draftId]: qty }));
  };

  const knownAccounts = useMemo(
    () => [...new Set((drafts.value ?? []).map((row) => row.content.sub_account_id))],
    [drafts.value],
  );

  const disabledNew = readOnly || replayActive || creatingBusy;

  async function handleStartNewDraft() {
    if (disabledNew) return;
    setCreatingBusy(true);
    setCreateError(undefined);
    try {
      const seed = await newManualDraft(activeInstrumentId, activeTimeframe);
      setCreatingSeed(seed);
    } catch (reason) {
      setCreateError(
        reason instanceof Error
          ? reason.message
          : t("冻结证据快照失败，无法新建草案。", "Failed to freeze evidence snapshot."),
      );
    } finally {
      setCreatingBusy(false);
    }
  }

  return (
    <section
      className="paper-draft-panel"
      aria-label={t("暂存交易草案", "Staged plan drafts")}
      data-testid="paper-draft-panel"
    >
      <header className="paper-draft-panel-header">
        <h2 className="paper-draft-panel-title">
          {t("暂存交易草案", "Staged Plan Drafts")}
          {drafts.value ? ` (${drafts.value.length})` : ""}
        </h2>
        <div className="paper-draft-panel-toolbar">
          <button
            type="button"
            className="primary-button"
            disabled={disabledNew}
            onClick={() => void handleStartNewDraft()}
          >
            {creatingBusy
              ? t("冻结快照…", "Freezing…")
              : t("+ 新建草案", "+ New draft")}
          </button>
          <button type="button" className="ghost-button" onClick={drafts.refresh}>
            {t("刷新", "Refresh")}
          </button>
        </div>
      </header>

      {replayActive && (
        <p className="paper-muted" role="status">
          {t(
            "回放模式下禁止新建或提交模拟盘草案。",
            "Paper draft creation and submission are disabled during replay.",
          )}
        </p>
      )}

      {createError && (
        <div className="paper-banner" role="alert">
          <span className="paper-error">{createError}</span>
        </div>
      )}

      {drafts.error && (
        <div className="paper-banner" role="alert" data-testid="paper-drafts-error">
          <span className="paper-error">
            {t("草案通道异常：", "Draft channel unavailable: ")}
            {drafts.error}
          </span>
          <button type="button" className="ghost-button" onClick={drafts.refresh}>
            {t("重试", "Retry")}
          </button>
        </div>
      )}

      {creatingSeed && !replayActive && !readOnly && (
        <DraftForm
          key={`${creatingSeed.snapshot_id}-${creatingSeed.nonce ?? "manual"}`}
          seed={creatingSeed}
          knownAccounts={knownAccounts}
          onClose={() => setCreatingSeed(null)}
          onSaved={(saved, qty) => {
            recordPreviewQty(saved.draft_id, qty ?? null);
            setCreatingSeed(null);
            refreshAndNotify();
          }}
        />
      )}

      {!drafts.value && !drafts.error && (
        <p className="paper-muted" role="status">
          {t("正在读取交易草案…", "Loading staged plan drafts…")}
        </p>
      )}

      {drafts.value && drafts.value.length === 0 && !creatingSeed && (
        <div className="paper-empty" data-testid="paper-drafts-empty">
          <p className="paper-muted">
            {t(
              "暂无暂存交易草案。点击“+ 新建草案”基于当前图表冻结证据快照并创建草案。",
              "No staged plan drafts yet. Click '+ New draft' to freeze a snapshot and stage one.",
            )}
          </p>
        </div>
      )}

      {drafts.value && drafts.value.length > 0 && (
        <div className="paper-draft-list">
          {drafts.value.map((draft) => (
            <DraftReview
              key={`${draft.draft_id}-${draft.version}-${draft.status}`}
              draft={draft}
              readOnly={readOnly || replayActive}
              highlighted={highlightedDraftId === draft.draft_id}
              knownAccounts={knownAccounts}
              previewedQuantity={previewedQtyByDraft[draft.draft_id] ?? null}
              onPreviewQuantityRecorded={recordPreviewQty}
              refresh={refreshAndNotify}
            />
          ))}
        </div>
      )}
    </section>
  );
}
