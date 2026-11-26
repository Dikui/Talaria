import { useCallback, useEffect, useId, useState, type FormEvent } from "react";
import type { DraftContent, DraftRecord, SizingPreviewResponse } from "@talaria/contracts";

import { api } from "../../lib/api";
import { t, useLocale } from "../../lib/i18n";
import {
  LAST_SUB_ACCOUNT_STORAGE_KEY,
  getLastSubAccount,
  setLastSubAccount,
} from "./useLastSubAccount";
import { usePaperQuery } from "./usePaperQuery";

export { LAST_SUB_ACCOUNT_STORAGE_KEY };

export type DraftSeed = Pick<DraftContent, "instrument_id" | "snapshot_id"> &
  Partial<DraftContent>;

export async function newManualDraft(
  instrument: string,
  timeframe: string,
  freeze = api.createSnapshot,
): Promise<DraftSeed> {
  const snapshot = await freeze(instrument, [timeframe]);
  return { instrument_id: snapshot.instrument_id, snapshot_id: snapshot.snapshot_id };
}

const ACCOUNT_ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const POSITIVE_DECIMAL_RE = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;

function isPositiveDecimal(raw: string): boolean {
  const trimmed = raw.trim();
  return POSITIVE_DECIMAL_RE.test(trimmed) && /[1-9]/.test(trimmed);
}

function formatPct(raw: string | null | undefined): string {
  if (!raw) return "—";
  const num = Number(raw);
  if (!Number.isFinite(num)) return raw;
  return `${(num * 100).toFixed(2)}%`;
}

function formatSizingRejection(reason: string): string {
  switch (reason) {
    case "INVALID_STOP_GEOMETRY":
      return t(
        "止损几何非法（多单止损须低于入场下界，空单止损须高于入场上界）",
        "Invalid stop geometry (LONG stop must be below entry lower; SHORT above entry upper)",
      );
    case "BELOW_MIN_QUANTITY":
      return t("计算数量低于标的最小下单量", "Sized quantity is below instrument minimum quantity");
    case "EXCEEDS_MAX_SINGLE_R":
      return t("超出单回合最大风险上限（max_single_r_pct）", "Exceeds max_single_r_pct single-round cap");
    case "INVALID_QUANTITY_STEP":
      return t("手动数量未按标的步长取整", "Manual quantity is not aligned to instrument qty_step");
    case "ACCOUNT_PROJECTION_UNAVAILABLE":
      return t("暂无该子账户状态投影，请先启动模拟盘账户进程", "No account projection found yet; start the paper account process");
    case "UNKNOWN_INSTRUMENT":
      return t("未知标的规格", "Unknown instrument specification");
    default:
      return reason;
  }
}

export function DraftForm({
  seed,
  draft,
  knownAccounts = [],
  previewDebounceMs = 120,
  onClose,
  onSaved,
}: {
  seed: DraftSeed;
  draft?: DraftRecord;
  knownAccounts?: readonly string[];
  previewDebounceMs?: number;
  onClose: () => void;
  onSaved: (record: DraftRecord, previewedQty?: string | null) => void;
}) {
  useLocale();
  const accounts = usePaperQuery(api.paperAccounts, 0);
  const positions = usePaperQuery(
    useCallback(() => api.paperBarriers(seed.instrument_id), [seed.instrument_id]),
    0,
  );
  const defaultAccount = seed.sub_account_id ?? getLastSubAccount();
  const [account, setAccount] = useState(defaultAccount);
  const [direction, setDirection] = useState<"LONG" | "SHORT">(seed.direction ?? "LONG");
  const [target, setTarget] = useState(
    seed.round_id && seed.inherited_barriers
      ? { id: seed.round_id, barriers: seed.inherited_barriers }
      : null,
  );
  const [entryLow, setEntryLow] = useState(seed.entry_low ?? "");
  const [entryHigh, setEntryHigh] = useState(seed.entry_high ?? "");
  const [stopLoss, setStopLoss] = useState(
    target?.barriers.stop ?? seed.invalidation_stop ?? "",
  );
  const [takeProfitText, setTakeProfitText] = useState(
    (target?.barriers.take_profit ?? seed.take_profit)?.join(", ") ?? "",
  );
  const [overrideQty, setOverrideQty] = useState("");
  const [preview, setPreview] = useState<SizingPreviewResponse | null>(null);
  const [previewError, setPreviewError] = useState<string>();
  const [previewLoading, setPreviewLoading] = useState(false);

  const matching =
    positions.value?.filter(
      (row) => row.sub_account_id === account && row.direction === direction && row.barriers,
    ) ?? [];
  const title = useId();
  const problem = useId();
  const accountListId = useId();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  const effectiveStop = target?.barriers.stop ?? stopLoss;

  useEffect(() => {
    const trimmedAccount = account.trim();
    const trimmedOverride = overrideQty.trim();
    if (
      !ACCOUNT_ID_RE.test(trimmedAccount) ||
      !isPositiveDecimal(entryLow) ||
      !isPositiveDecimal(entryHigh) ||
      !isPositiveDecimal(effectiveStop) ||
      (trimmedOverride && !isPositiveDecimal(trimmedOverride))
    ) {
      setPreview(null);
      setPreviewError(undefined);
      setPreviewLoading(false);
      return;
    }

    const targets = takeProfitText
      .split(/[、,，\s]+/)
      .filter((item) => isPositiveDecimal(item))
      .slice(0, 4);

    let cancelled = false;
    setPreviewLoading(true);
    const timer = setTimeout(async () => {
      try {
        const res = await api.previewPaperSizing({
          sub_account_id: trimmedAccount,
          instrument_id: seed.instrument_id,
          direction,
          entry_low: entryLow.trim(),
          entry_high: entryHigh.trim(),
          invalidation_stop: effectiveStop.trim(),
          take_profit: targets,
          round_id: target?.id ?? null,
          override_quantity: trimmedOverride ? trimmedOverride : null,
        });
        if (!cancelled) {
          setPreview(res);
          setPreviewError(undefined);
        }
      } catch (reason) {
        if (!cancelled) {
          setPreview(null);
          setPreviewError(
            reason instanceof Error
              ? reason.message
              : t("定仓预演服务不可用", "Sizing preview unavailable"),
          );
        }
      } finally {
        if (!cancelled) setPreviewLoading(false);
      }
    }, previewDebounceMs);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [
    account,
    direction,
    entryLow,
    entryHigh,
    effectiveStop,
    takeProfitText,
    overrideQty,
    target?.id,
    seed.instrument_id,
    previewDebounceMs,
  ]);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const form = event.currentTarget;
    const data = new FormData(form);
    const value = (key: string) => String(data.get(key) ?? "").trim();
    const trimmedAccount = account.trim();
    const targets = takeProfitText
      .split(/[、,，\s]+/)
      .filter(Boolean);
    const ttlMinutesRaw = value("valid_minutes");
    let validUntil = seed.valid_until ?? null;
    if (ttlMinutesRaw) {
      const minutes = Number(ttlMinutesRaw);
      if (!Number.isFinite(minutes) || minutes <= 0) {
        setError(t("有效分钟数须为正数。", "Validity minutes must be a positive number."));
        return;
      }
      validUntil = Date.now() + Math.round(minutes * 60_000);
    }

    const content: DraftContent = {
      instrument_id: seed.instrument_id,
      snapshot_id: seed.snapshot_id,
      consultation_id: seed.consultation_id ?? null,
      valid_until: validUntil,
      sub_account_id: trimmedAccount,
      round_id: target?.id ?? null,
      inherited_barriers: target?.barriers ?? null,
      direction,
      entry_low: entryLow.trim(),
      entry_high: entryHigh.trim(),
      invalidation_stop: effectiveStop.trim(),
      take_profit: targets,
    };

    if (!ACCOUNT_ID_RE.test(content.sub_account_id)) {
      setError(
        t(
          "请输入合法子账户（小写字母/数字开头，可含 _ 或 -）。",
          "Enter a valid sub-account ID (lowercase letters/digits, optional _ or -).",
        ),
      );
      (form.elements.namedItem("sub_account_id") as HTMLElement | null)?.focus();
      return;
    }

    const priceFields = [
      "entry_low",
      "entry_high",
      "invalidation_stop",
      "take_profit",
    ] as const;
    const invalidPrice = priceFields.find((field) => {
      const items = field === "take_profit" ? targets : [content[field]];
      if (field === "take_profit" && (items.length === 0 || items.length > 4)) return true;
      return !items.every((item) => isPositiveDecimal(item));
    });
    if (invalidPrice) {
      setError(
        t(
          "价格须为正十进制数，最多 4 个止盈价（逗号分隔）。",
          "Prices must be positive decimals; up to 4 comma-separated take-profit targets.",
        ),
      );
      (form.elements.namedItem(invalidPrice) as HTMLElement | null)?.focus();
      return;
    }

    setBusy(true);
    setError(undefined);
    try {
      const saved = draft
        ? await api.editPaperDraft(draft.draft_id, { version: draft.version, content })
        : await api.savePaperDraft(content);
      setLastSubAccount(content.sub_account_id);
      onSaved(saved, preview?.rejected_reason ? null : (preview?.quantity ?? null));
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : t("保存失败，请重试。", "Save failed. Please try again."),
      );
      setBusy(false);
    }
  }

  const options = [
    ...new Set([
      ...(seed.sub_account_id ? [seed.sub_account_id] : []),
      ...(accounts.value ?? []),
      ...knownAccounts,
    ]),
  ];

  return (
    <form
      className="paper-card paper-draft-form"
      noValidate
      onSubmit={save}
      aria-labelledby={title}
      aria-busy={busy}
      aria-describedby={error ? problem : undefined}
    >
      <h3 id={title} className="paper-draft-panel-title">
        {draft ? t("编辑交易草案", "Edit plan draft") : t("新建交易草案", "New plan draft")}
      </h3>
      <p className="paper-instrument">{seed.instrument_id}</p>
      <p className="paper-muted">
        {t("证据快照", "Evidence snapshot")}: {seed.snapshot_id}
      </p>
      <div className="paper-form-fields">
        <label>
          {t("子账户", "Sub-account")}
          {options.length > 0 ? (
            <>
              <input
                name="sub_account_id"
                list={accountListId}
                value={account}
                placeholder="sa-paper"
                onChange={(event) => {
                  setAccount(event.target.value);
                  setTarget(null);
                }}
                required
              />
              <datalist id={accountListId}>
                {options.map((item) => (
                  <option key={item} value={item} />
                ))}
              </datalist>
            </>
          ) : (
            <input
              name="sub_account_id"
              value={account}
              placeholder="sa-paper"
              onChange={(event) => {
                setAccount(event.target.value);
                setTarget(null);
              }}
              required
            />
          )}
        </label>
        <label>
          {t("方向", "Direction")}
          <select
            name="direction"
            value={direction}
            onChange={(event) => {
              setDirection(event.target.value === "SHORT" ? "SHORT" : "LONG");
              setTarget(null);
            }}
          >
            <option value="LONG">{t("做多", "Long")}</option>
            <option value="SHORT">{t("做空", "Short")}</option>
          </select>
        </label>
        <label>
          {t("目标回合", "Target round")}
          <select
            value={target?.id ?? ""}
            onChange={(event) => {
              const row = matching.find((item) => item.round_id === event.target.value);
              const next = row?.barriers ? { id: row.round_id, barriers: row.barriers } : null;
              setTarget(next);
              if (next) {
                setStopLoss(next.barriers.stop);
                setTakeProfitText(next.barriers.take_profit.join(", "));
              }
            }}
          >
            <option value="">{t("新开回合", "New round")}</option>
            {target && !matching.some((row) => row.round_id === target.id) && (
              <option value={target.id}>{target.id}</option>
            )}
            {matching.map((row) => (
              <option key={row.round_id} value={row.round_id}>
                {row.round_id}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("入场下界", "Entry lower")}
          <input
            name="entry_low"
            inputMode="decimal"
            value={entryLow}
            onChange={(event) => setEntryLow(event.target.value)}
            required
          />
        </label>
        <label>
          {t("入场上界", "Entry upper")}
          <input
            name="entry_high"
            inputMode="decimal"
            value={entryHigh}
            onChange={(event) => setEntryHigh(event.target.value)}
            required
          />
        </label>
        <label>
          {t("止损", "Stop loss")}
          <input
            name="invalidation_stop"
            inputMode="decimal"
            readOnly={!!target}
            value={effectiveStop}
            onChange={(event) => setStopLoss(event.target.value)}
            required
          />
        </label>
        <label>
          {t("止盈（逗号分隔）", "Take profit (comma separated)")}
          <input
            name="take_profit"
            readOnly={!!target}
            value={takeProfitText}
            onChange={(event) => setTakeProfitText(event.target.value)}
            required
          />
        </label>
        <label>
          {t("手动数量核对（留空按 R 定仓）", "Manual qty check (optional)")}
          <input
            name="override_quantity"
            inputMode="decimal"
            placeholder={t("自动定仓", "Auto R-unit")}
            value={overrideQty}
            onChange={(event) => setOverrideQty(event.target.value)}
          />
        </label>
        <label>
          {t("有效期（分钟，留空不过期）", "Valid for (min, optional)")}
          <input name="valid_minutes" inputMode="numeric" placeholder="15" />
        </label>
      </div>

      {(previewLoading || preview || previewError) && (
        <section
          className="paper-sizing-preview"
          aria-label={t("定仓实时预演", "Live sizing preview")}
          data-testid="paper-sizing-preview"
        >
          <header className="paper-sizing-header">
            <strong>{t("定仓实时预演", "Sizing Preview")}</strong>
            <div className="paper-sizing-badges">
              {previewLoading && (
                <span className="paper-muted">{t("计算中…", "Calculating…")}</span>
              )}
              {preview?.from_offline_snapshot && (
                <span
                  className="paper-offline-badge"
                  data-testid="paper-sizing-offline-badge"
                >
                  {t("基于离线快照", "Based on offline snapshot")}
                </span>
              )}
            </div>
          </header>

          {previewError && (
            <p className="paper-error" role="alert">
              {previewError}
            </p>
          )}

          {preview?.rejected_reason && (
            <div
              className="paper-sizing-rejection"
              role="alert"
              data-testid="paper-sizing-rejection"
            >
              <strong>{t("预演拒绝", "Preview rejected")}: </strong>
              <span>{formatSizingRejection(preview.rejected_reason)}</span>
            </div>
          )}

          {preview && (preview.quantity || preview.risk_amount_r || preview.est_liq_price) && (
            <dl className="paper-sizing-grid">
              <div>
                <dt>{t("预估数量", "Quantity")}</dt>
                <dd data-testid="paper-sizing-qty">{preview.quantity ?? "—"}</dd>
              </div>
              <div>
                <dt>{t("风险额 (R)", "Risk (R)")}</dt>
                <dd data-testid="paper-sizing-risk">
                  {preview.risk_amount_r ?? "—"}
                  {preview.risk_pct_of_equity ? ` (${formatPct(preview.risk_pct_of_equity)})` : ""}
                </dd>
              </div>
              <div>
                <dt>{t("预估强平价", "Est. liquidation")}</dt>
                <dd data-testid="paper-sizing-liq">{preview.est_liq_price ?? "—"}</dd>
              </div>
              <div>
                <dt>{t("止损距强平", "Stop to liq.")}</dt>
                <dd data-testid="paper-sizing-liq-dist">
                  {preview.est_liq_distance ?? "—"}
                  {preview.est_liq_distance_pct
                    ? ` (${formatPct(preview.est_liq_distance_pct)})`
                    : ""}
                </dd>
              </div>
            </dl>
          )}

          {preview?.basis && (
            <p className="paper-sizing-basis" data-testid="paper-sizing-basis">
              {t("计算依据：", "Basis: ")}
              {preview.basis}
            </p>
          )}
        </section>
      )}

      {accounts.value?.length === 0 && !account && (
        <p className="paper-muted" role="status">
          {t(
            "当前未发现已运行子账户；可直接输入目标子账户 ID 保存草案，待账户进程启动后领取。",
            "No running sub-accounts detected yet; enter a target sub-account ID to stage a draft.",
          )}
        </p>
      )}
      {error && (
        <p id={problem} className="paper-error" role="alert">
          {error}
        </p>
      )}
      <div className="paper-actions-row">
        <button type="submit" className="primary-button" disabled={busy}>
          {busy ? t("正在保存…", "Saving…") : t("保存草案", "Save draft")}
        </button>
        <button type="button" className="ghost-button" disabled={busy} onClick={onClose}>
          {t("取消", "Cancel")}
        </button>
      </div>
    </form>
  );
}
