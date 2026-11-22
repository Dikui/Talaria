import { useCallback, useId, useState, type FormEvent, type ReactNode } from "react";
import type { DraftRecord, PaperAccountView, PaperRound } from "@talaria/contracts";
import { api } from "../../lib/api";
import type { Theme } from "../../lib/theme";
import { DraftForm } from "./DraftForm";
import { usePaperQuery } from "./usePaperQuery";

const PHASES: Record<string, string> = {
  PENDING_ENTRY: "等待成交", PARTIALLY_FILLED_PROTECTED: "部分成交", ACTIVE_PROTECTED: "持仓中",
  EXITING: "正在平仓", CLOSED: "已平仓", CANCELLED: "已撤单", LIQUIDATED: "已强平",
};

const formatTime = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});

function RoundRows({ rows, empty }: { rows: PaperRound[]; empty: string }) {
  return rows.length === 0 ? <p className="paper-muted">{empty}</p> : (
    <ul className="paper-rows">
      {rows.map((row) => <li key={row.round_id}>
        <header><strong className="paper-instrument">{row.instrument_id}</strong>
          <span>{PHASES[row.phase ?? ""] ?? row.phase}</span></header>
        <p className="paper-muted">{row.round_id}</p>
        <dl>
          <div><dt>方向</dt><dd>{row.direction === "LONG" ? "做多" : "做空"}</dd></div>
          <div><dt>剩余数量</dt><dd>{row.quantity}</dd></div>
          <div><dt>止损</dt><dd>{row.stop}</dd></div>
          {(row.take_profit?.length ?? 0) > 0 && <div><dt>止盈</dt><dd>{row.take_profit?.join(" / ")}</dd></div>}
        </dl>
      </li>)}
    </ul>
  );
}

export function LedgerSections({ view }: { view: PaperAccountView }) {
  return (
    <div className="paper-ledger-grid">
      <section className="paper-card paper-positions">
        <h2>持仓 <span>{view.positions.length}</span></h2>
        <RoundRows rows={view.positions} empty="当前无持仓" />
      </section>
      <section className="paper-card">
        <h2>仓位回合 <span>{view.rounds.length}</span></h2>
        <RoundRows rows={view.rounds} empty="暂无仓位回合" />
      </section>
      <section className="paper-card">
        <h2>成交 <span>{view.fills.length}</span></h2>
        {view.fills.length === 0 ? <p className="paper-muted">暂无成交</p> : <ul className="paper-rows">
          {view.fills.map((row) => <li key={row.event_id}>
            <header><strong className="paper-instrument">{row.instrument_id}</strong>
              <span>{row.kind === "ENTRY_FILLED" ? "入场" : "出场"}</span></header>
            <p className="paper-muted">
              <time dateTime={new Date(row.ts_event).toISOString()}>{formatTime.format(row.ts_event)}</time>
              {" · "}{row.round_id}
            </p>
            <dl>
              <div><dt>成交价</dt><dd>{row.price}</dd></div>
              <div><dt>数量</dt><dd>{row.quantity}</dd></div>
            </dl>
          </li>)}
        </ul>}
      </section>
    </div>
  );
}

function DraftReview({ draft, refresh }: { draft: DraftRecord; refresh: () => void }) {
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
    const confirmedBy = field.value.trim();
    if (!confirmedBy) {
      setError("请输入确认人。");
      field.focus();
      return;
    }
    setBusy(true);
    setError(undefined);
    try {
      await api.confirmPaperDraft(draft.draft_id, {
        version: draft.version, content_hash: draft.content_hash, confirmed_by: confirmedBy,
      });
      refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "确认失败，请重试。");
      refresh();
    } finally {
      setBusy(false);
    }
  }

  const content = draft.content;
  const receipt = draft.receipt;
  const status = receipt?.accepted ? "已通过定仓与风控" : receipt
    ? receipt.reason === "DRAFT_STALE" ? "草案已过期" : `账户拒绝 · ${receipt.reason ?? "未知原因"}`
    : draft.claimed ? "账户处理中" : draft.status === "CONFIRMED" ? "已确认 · 等待账户接收" : "待确认";
  return <article className="paper-draft-review">
    <header>
      <strong className="paper-instrument">{content.instrument_id}</strong>
      <span className={receipt && !receipt.accepted ? "paper-error" : undefined}>{status}</span>
    </header>
    <p className="paper-muted">版本 {draft.version} · {content.consultation_id ? "协同会诊" : "人工"} · {content.direction === "LONG" ? "做多" : "做空"}</p>
    <dl>
      <div><dt>入场价带</dt><dd>{content.entry_low}–{content.entry_high}</dd></div>
      <div><dt>止损</dt><dd>{content.invalidation_stop}</dd></div>
      <div><dt>止盈</dt><dd>{content.take_profit.join(" / ")}</dd></div>
    </dl>
    {!draft.claimed && <form className="paper-actions" noValidate onSubmit={confirm} aria-busy={busy}>
      {draft.status !== "CONFIRMED" && <>
        <label>确认人<input name="confirmed_by" maxLength={80} required aria-describedby={error ? problem : undefined} /></label>
        <button type="submit" className="primary-button" disabled={busy}>{busy ? "正在确认…" : "确认并提交模拟盘"}</button>
      </>}
      <button type="button" className="ghost-button" disabled={busy} onClick={() => setEditing(draft)}>编辑草案</button>
    </form>}
    {error && <p id={problem} className="paper-error" role="alert">{error}</p>}
  </article>;
}

function AccountLedger({ account }: { account: string }) {
  const query = useCallback(() => api.paperAccount(account), [account]);
  const ledger = usePaperQuery(query);
  return ledger.error
    ? <p role="alert">{ledger.error} <button type="button" className="ghost-button" onClick={ledger.refresh}>重试</button></p>
    : ledger.value ? <LedgerSections view={ledger.value} /> : <p role="status">正在读取账本…</p>;
}

export function AccountPanel({ theme, tools }: { theme: Theme; tools?: ReactNode }) {
  const accounts = usePaperQuery(api.paperAccounts);
  const drafts = usePaperQuery(api.paperDrafts);
  const [selected, setSelected] = useState("");
  const identifiers = [...new Set([...(accounts.value ?? []), ...(drafts.value ?? []).map((row) => row.content.sub_account_id)])];
  const account = identifiers.includes(selected) ? selected : identifiers[0] ?? "";
  const matching = (drafts.value ?? []).filter((row) => row.content.sub_account_id === account);

  return <section className="talaria-desk feature-workbench paper-panel" data-theme={theme} aria-label="模拟盘账户">
    <header className="feature-workbench-header">
      <div className="feature-workbench-title"><h1>模拟盘账户</h1>{tools}</div>
      {identifiers.length > 0 && <label className="paper-account-select">子账户
        <select value={account} onChange={(event) => setSelected(event.target.value)}>
          {identifiers.map((id) => <option key={id}>{id}</option>)}
        </select>
      </label>}
    </header>
    <div className="feature-workbench-body">
      {accounts.error && <p role="alert">{accounts.error} <button type="button" className="ghost-button" onClick={accounts.refresh}>重试</button></p>}
      {!accounts.value && !accounts.error && <p role="status">正在读取子账户…</p>}
      {accounts.value && (accounts.value.includes(account)
        ? <AccountLedger key={account} account={account} />
        : <p className="paper-muted">暂无模拟盘账本</p>)}
      {account !== "" && <section className="paper-card paper-drafts">
        <h2>交易草案</h2>
        {drafts.error ? <p role="alert">{drafts.error} <button type="button" className="ghost-button" onClick={drafts.refresh}>重试</button></p>
          : !drafts.value ? <p role="status">正在读取草案…</p>
            : matching.length === 0 ? <p className="paper-muted">暂无交易草案</p>
              : matching.map((draft) => <DraftReview key={draft.draft_id} draft={draft} refresh={drafts.refresh} />)}
      </section>}
    </div>
  </section>;
}
