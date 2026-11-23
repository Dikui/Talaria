import { useCallback, useId, useState, type FormEvent, type ReactNode } from "react";
import type { DraftRecord, PaperAccountView, PaperRound } from "@talaria/contracts";
import { api } from "../../lib/api";
import { getLocale, t, useLocale } from "../../lib/i18n";
import type { Theme } from "../../lib/theme";
import { DraftForm } from "./DraftForm";
import { usePaperQuery } from "./usePaperQuery";

function phaseLabel(phase?: string | null): string {
  switch (phase) {
    case "PENDING_ENTRY": return t("等待成交", "Awaiting fill");
    case "PARTIALLY_FILLED_PROTECTED": return t("部分成交", "Partially filled");
    case "ACTIVE_PROTECTED": return t("持仓中", "In position");
    case "EXITING": return t("正在平仓", "Exiting");
    case "CLOSED": return t("已平仓", "Closed");
    case "CANCELLED": return t("已撤单", "Cancelled");
    case "LIQUIDATED": return t("已强平", "Liquidated");
    default: return phase ?? "";
  }
}

function formatTime(ts: number): string {
  return new Intl.DateTimeFormat(getLocale() === "zh" ? "zh-CN" : "en-US", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(ts);
}

function RoundRows({ rows, empty }: { rows: PaperRound[]; empty: string }) {
  return rows.length === 0 ? <p className="paper-muted">{empty}</p> : (
    <ul className="paper-rows">
      {rows.map((row) => <li key={row.round_id}>
        <header><strong className="paper-instrument">{row.instrument_id}</strong>
          <span>{phaseLabel(row.phase)}</span></header>
        <p className="paper-muted">{row.round_id}</p>
        <dl>
          <div><dt>{t("方向", "Direction")}</dt><dd>{row.direction === "LONG" ? t("做多", "Long") : t("做空", "Short")}</dd></div>
          <div><dt>{t("剩余数量", "Remaining qty")}</dt><dd>{row.quantity}</dd></div>
          <div><dt>{t("止损", "Stop loss")}</dt><dd>{row.stop}</dd></div>
          {(row.take_profit?.length ?? 0) > 0 && <div><dt>{t("止盈", "Take profit")}</dt><dd>{row.take_profit?.join(" / ")}</dd></div>}
        </dl>
      </li>)}
    </ul>
  );
}

export function LedgerSections({ view }: { view: PaperAccountView }) {
  useLocale();
  return (
    <div className="paper-ledger-grid">
      <section className="paper-card paper-positions">
        <h2>{t("持仓", "Positions")} <span>{view.positions.length}</span></h2>
        <RoundRows rows={view.positions} empty={t("当前无持仓", "No open positions")} />
      </section>
      <section className="paper-card">
        <h2>{t("仓位回合", "Position rounds")} <span>{view.rounds.length}</span></h2>
        <RoundRows rows={view.rounds} empty={t("暂无仓位回合", "No position rounds yet")} />
      </section>
      <section className="paper-card">
        <h2>{t("成交", "Fills")} <span>{view.fills.length}</span></h2>
        {view.fills.length === 0 ? <p className="paper-muted">{t("暂无成交", "No fills yet")}</p> : <ul className="paper-rows">
          {view.fills.map((row) => <li key={row.event_id}>
            <header><strong className="paper-instrument">{row.instrument_id}</strong>
              <span>{row.kind === "ENTRY_FILLED" ? t("入场", "Entry") : t("出场", "Exit")}</span></header>
            <p className="paper-muted">
              <time dateTime={new Date(row.ts_event).toISOString()}>{formatTime(row.ts_event)}</time>
              {" · "}{row.round_id}
            </p>
            <dl>
              <div><dt>{t("成交价", "Fill price")}</dt><dd>{row.price}</dd></div>
              <div><dt>{t("数量", "Quantity")}</dt><dd>{row.quantity}</dd></div>
            </dl>
          </li>)}
        </ul>}
      </section>
    </div>
  );
}

export function DraftReview({ draft, refresh }: { draft: DraftRecord; refresh: () => void }) {
  useLocale();
  const [editing, setEditing] = useState<DraftRecord>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const problem = useId();

  if (editing && !draft.claimed) {
    return <DraftForm seed={editing.content} draft={editing} onClose={() => setEditing(undefined)} onSaved={() => {
      setEditing(undefined);
      refresh();
    }} />;
  }

  async function confirm(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const field = event.currentTarget.elements.namedItem("confirmed_by") as HTMLInputElement;
    const reviewed = new FormData(event.currentTarget);
    const confirmedBy = field.value.trim();
    if (!confirmedBy) {
      setError(t("请输入确认人。", "Enter a confirmer."));
      field.focus();
      return;
    }
    setBusy(true);
    setError(undefined);
    try {
      await api.confirmPaperDraft(draft.draft_id, {
        version: Number(reviewed.get("version")), content_hash: String(reviewed.get("content_hash")), confirmed_by: confirmedBy,
      });
      refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("确认失败，请重试。", "Confirmation failed. Please try again."));
      refresh();
    } finally {
      setBusy(false);
    }
  }

  const content = draft.content;
  const receipt = draft.receipt;
  const status = receipt?.accepted
    ? t("已通过定仓与风控", "Passed sizing and risk checks")
    : receipt
      ? receipt.reason === "DRAFT_STALE"
        ? t("草案已过期", "Draft expired")
        : t(`账户拒绝 · ${receipt.reason ?? "未知原因"}`, `Account rejected · ${receipt.reason ?? "unknown reason"}`)
      : draft.claimed
        ? t("账户处理中", "Account processing")
        : draft.status === "CONFIRMED"
          ? t("已确认 · 等待账户接收", "Confirmed · awaiting account")
          : t("待确认", "Pending confirmation");
  return <article className="paper-draft-review">
    <header>
      <strong className="paper-instrument">{content.instrument_id}</strong>
      <span className={receipt && !receipt.accepted ? "paper-error" : undefined}>{status}</span>
    </header>
    <p className="paper-muted">{t("版本", "Version")} {draft.version} · {content.consultation_id ? t("协同会诊", "Consultation") : t("人工", "Manual")} · {content.direction === "LONG" ? t("做多", "Long") : t("做空", "Short")}</p>
    <dl>
      {receipt?.plan_id && <div><dt>{t("执行计划", "Execution plan")}</dt><dd>{receipt.plan_id}</dd></div>}
      {content.inherited_barriers && <>
        <div><dt>{t("加仓回合", "Scaling round")}</dt><dd>{content.round_id}</dd></div>
        <div><dt>{t("最长持仓", "Max holding")}</dt><dd>{content.inherited_barriers.max_holding_ms / 1000} {t("秒", "s")}</dd></div>
        <div><dt>{t("移动止损", "Trailing stop")}</dt><dd>{content.inherited_barriers.trailing_stop
          ? t(
              `激活幅度 ${content.inherited_barriers.trailing_stop.activation_pct}，回撤幅度 ${content.inherited_barriers.trailing_stop.distance_pct}`,
              `activation ${content.inherited_barriers.trailing_stop.activation_pct}, distance ${content.inherited_barriers.trailing_stop.distance_pct}`,
            )
          : t("未启用", "Disabled")}</dd></div>
      </>}
      <div><dt>{t("证据快照", "Evidence snapshot")}</dt><dd>{content.snapshot_id}</dd></div>
      <div><dt>{t("有效期", "Valid until")}</dt><dd>{content.valid_until == null ? t("未设截止时刻", "No deadline") :
        <time dateTime={new Date(content.valid_until).toISOString()}>{new Date(content.valid_until).toLocaleString(getLocale() === "zh" ? "zh-CN" : "en-US")}</time>}</dd></div>
      <div><dt>{t("入场价带", "Entry band")}</dt><dd>{content.entry_low}–{content.entry_high}</dd></div>
      <div><dt>{t("止损", "Stop loss")}</dt><dd>{content.invalidation_stop}</dd></div>
      <div><dt>{t("止盈", "Take profit")}</dt><dd>{content.take_profit.join(" / ")}</dd></div>
    </dl>
    {!draft.claimed && <form className="paper-actions" noValidate onSubmit={confirm} aria-busy={busy}>
      {draft.status !== "CONFIRMED" && <>
        <input type="hidden" name="version" value={draft.version} />
        <input type="hidden" name="content_hash" value={draft.content_hash} />
        <label>{t("确认人", "Confirmer")}<input name="confirmed_by" maxLength={80} required aria-describedby={error ? problem : undefined} /></label>
        <button type="submit" className="primary-button" disabled={busy}>{busy ? t("正在确认…", "Confirming…") : t("确认并提交模拟盘", "Confirm and submit to paper")}</button>
      </>}
      <button type="button" className="ghost-button" disabled={busy} onClick={() => setEditing(draft)}>{t("编辑草案", "Edit draft")}</button>
    </form>}
    {error && <p id={problem} className="paper-error" role="alert">{error}</p>}
  </article>;
}

function AccountLedger({ account }: { account: string }) {
  const query = useCallback(() => api.paperAccount(account), [account]);
  const ledger = usePaperQuery(query);
  useLocale();
  return ledger.error
    ? <p role="alert">{ledger.error} <button type="button" className="ghost-button" onClick={ledger.refresh}>{t("重试", "Retry")}</button></p>
    : ledger.value ? <LedgerSections view={ledger.value} /> : <p role="status">{t("正在读取账本…", "Loading ledger…")}</p>;
}

export function AccountPanel({ theme, tools }: { theme: Theme; tools?: ReactNode }) {
  useLocale();
  const accounts = usePaperQuery(api.paperAccounts);
  const drafts = usePaperQuery(api.paperDrafts);
  const [selected, setSelected] = useState("");
  const identifiers = [...new Set([...(accounts.value ?? []), ...(drafts.value ?? []).map((row) => row.content.sub_account_id)])];
  const account = identifiers.includes(selected) ? selected : identifiers[0] ?? "";
  const matching = (drafts.value ?? []).filter((row) => row.content.sub_account_id === account);

  return <section className="talaria-desk feature-workbench paper-panel" data-theme={theme} aria-label={t("模拟盘账户", "Paper account")}>
    <header className="feature-workbench-header">
      <div className="feature-workbench-title"><h1>{t("模拟盘账户", "Paper account")}</h1>{tools}</div>
      {identifiers.length > 0 && <label className="paper-account-select">{t("子账户", "Sub-account")}
        <select value={account} onChange={(event) => setSelected(event.target.value)}>
          {identifiers.map((id) => <option key={id}>{id}</option>)}
        </select>
      </label>}
    </header>
    <div className="feature-workbench-body">
      {accounts.error && <p role="alert">{accounts.error} <button type="button" className="ghost-button" onClick={accounts.refresh}>{t("重试", "Retry")}</button></p>}
      {!accounts.value && !accounts.error && <p role="status">{t("正在读取子账户…", "Loading sub-accounts…")}</p>}
      {accounts.value && (accounts.value.includes(account)
        ? <AccountLedger key={account} account={account} />
        : <p className="paper-muted">{t("暂无模拟盘账本", "No paper ledger")}</p>)}
      {account !== "" && <section className="paper-card paper-drafts">
        <h2>{t("交易草案", "Trading drafts")}</h2>
        {drafts.error ? <p role="alert">{drafts.error} <button type="button" className="ghost-button" onClick={drafts.refresh}>{t("重试", "Retry")}</button></p>
          : !drafts.value ? <p role="status">{t("正在读取草案…", "Loading drafts…")}</p>
            : matching.length === 0 ? <p className="paper-muted">{t("暂无交易草案", "No trading drafts")}</p>
              : matching.map((draft) => <DraftReview key={`${draft.draft_id}:${draft.version}:${draft.content_hash}`} draft={draft} refresh={drafts.refresh} />)}
      </section>}
    </div>
  </section>;
}
