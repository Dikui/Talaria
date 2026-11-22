import { useCallback, useId, useState, type FormEvent } from "react";
import type { DraftContent, DraftRecord } from "@talaria/contracts";
import { api } from "../../lib/api";
import { usePaperQuery } from "./usePaperQuery";

export type DraftSeed = Pick<DraftContent, "instrument_id" | "snapshot_id"> & Partial<DraftContent>;

export async function newManualDraft(instrument: string, timeframe: string, freeze = api.createSnapshot): Promise<DraftSeed> {
  const snapshot = await freeze(instrument, [timeframe]);
  return { instrument_id: snapshot.instrument_id, snapshot_id: snapshot.snapshot_id };
}

export function DraftForm({ seed, draft, onClose, onSaved }: {
  seed: DraftSeed;
  draft?: DraftRecord;
  onClose: () => void;
  onSaved: (record: DraftRecord) => void;
}) {
  const accounts = usePaperQuery(api.paperAccounts);
  const positions = usePaperQuery(useCallback(() => api.paperBarriers(seed.instrument_id), [seed.instrument_id]));
  const [account, setAccount] = useState(seed.sub_account_id ?? "");
  const [direction, setDirection] = useState(seed.direction ?? "LONG");
  const [target, setTarget] = useState(seed.round_id && seed.inherited_barriers
    ? { id: seed.round_id, barriers: seed.inherited_barriers } : null);
  const matching = positions.value?.filter(row =>
    row.sub_account_id === account && row.direction === direction && row.barriers) ?? [];
  const title = useId();
  const problem = useId();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const form = event.currentTarget;
    const data = new FormData(form);
    const value = (key: string) => String(data.get(key) ?? "").trim();
    const targets = value("take_profit").split(/[、,，\s]+/).filter(Boolean);
    const content: DraftContent = {
      instrument_id: seed.instrument_id, snapshot_id: seed.snapshot_id,
      consultation_id: seed.consultation_id, valid_until: seed.valid_until,
      sub_account_id: account, round_id: target?.id, inherited_barriers: target?.barriers,
      direction: value("direction") === "SHORT" ? "SHORT" : "LONG",
      entry_low: value("entry_low"), entry_high: value("entry_high"),
      invalidation_stop: value("invalidation_stop"), take_profit: targets,
    };
    const invalid = (["sub_account_id", "entry_low", "entry_high", "invalidation_stop", "take_profit"] as const)
      .find((field) => field === "sub_account_id"
        ? !content.sub_account_id
        : ![content[field]].flat().every((item) => /^\d+(\.\d+)?$/.test(item) && /[1-9]/.test(item))
          || (field === "take_profit" && targets.length === 0));
    if (invalid) {
      setError(invalid === "sub_account_id" ? "请选择子账户。" : "价格须为正数。多个止盈价可用逗号分隔。");
      (form.elements.namedItem(invalid) as HTMLElement | null)?.focus();
      return;
    }
    setBusy(true);
    setError(undefined);
    try {
      const saved = draft
        ? await api.editPaperDraft(draft.draft_id, { version: draft.version, content })
        : await api.savePaperDraft(content);
      onSaved(saved);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败，请重试。");
      setBusy(false);
    }
  }

  const options = [...new Set([...(seed.sub_account_id ? [seed.sub_account_id] : []), ...(accounts.value ?? [])])];
  return (
    <form className="paper-card paper-draft-form" noValidate onSubmit={save} aria-labelledby={title}
      aria-busy={busy} aria-describedby={error ? problem : undefined}>
      <h2 id={title}>{draft ? "编辑模拟盘草案" : "新建模拟盘草案"}</h2>
      <p className="paper-instrument">{seed.instrument_id}</p>
      <div className="paper-form-fields">
        <label>子账户
          <select name="sub_account_id" value={account} onChange={event => { setAccount(event.target.value); setTarget(null); }} required>
            <option value="">请选择</option>
            {options.map((account) => <option key={account}>{account}</option>)}
          </select>
        </label>
        <label>方向
          <select name="direction" value={direction} onChange={event => { setDirection(event.target.value === "SHORT" ? "SHORT" : "LONG"); setTarget(null); }}>
            <option value="LONG">做多</option><option value="SHORT">做空</option>
          </select>
        </label>
        <label>目标回合
          <select value={target?.id ?? ""} onChange={event => {
            const row = matching.find(item => item.round_id === event.target.value);
            setTarget(row?.barriers ? { id: row.round_id, barriers: row.barriers } : null);
          }}>
            <option value="">新开回合</option>
            {target && !matching.some(row => row.round_id === target.id) && <option>{target.id}</option>}
            {matching.map(row => <option key={row.round_id}>{row.round_id}</option>)}
          </select>
        </label>
        <label>入场下界<input name="entry_low" inputMode="decimal" defaultValue={seed.entry_low} required /></label>
        <label>入场上界<input name="entry_high" inputMode="decimal" defaultValue={seed.entry_high} required /></label>
        <label>止损<input key={target?.id ?? ""} name="invalidation_stop" inputMode="decimal" readOnly={!!target} defaultValue={target?.barriers.stop ?? seed.invalidation_stop} required /></label>
        <label>止盈（逗号分隔）<input key={target?.id ?? ""} name="take_profit" readOnly={!!target} defaultValue={(target?.barriers.take_profit ?? seed.take_profit)?.join(", ")} required /></label>
      </div>
      {positions.error && <p role="status">无法读取加仓回合。 <button type="button" onClick={positions.refresh}>重试</button></p>}
      {accounts.error && <p role="status">无法读取子账户。 <button type="button" className="ghost-button" onClick={accounts.refresh}>重试</button></p>}
      {accounts.value?.length === 0 && !seed.sub_account_id && <p role="status">暂无子账户。启动模拟盘后即可选择账户。</p>}
      {error && <p id={problem} className="paper-error" role="alert">{error}</p>}
      <div className="paper-actions">
        <button type="submit" className="primary-button" disabled={busy}>{busy ? "正在保存…" : "保存草案"}</button>
        <button type="button" className="ghost-button" disabled={busy} onClick={onClose}>取消</button>
      </div>
    </form>
  );
}
