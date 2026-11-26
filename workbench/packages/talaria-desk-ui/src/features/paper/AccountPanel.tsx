import { useCallback, useEffect, useState } from "react";
import type {
  PaperAccountState,
  PaperAccountView,
  PaperCommand,
  PaperCommandKind,
  PaperCommandPayload,
  PaperCommandRecord,
  PaperRound,
  TradingState,
} from "@talaria/contracts";

import { api } from "../../lib/api";
import { getLocale, t, useLocale } from "../../lib/i18n";
import { PaperProcessControl } from "./PaperProcessControl";
import {
  isPaperProcessOnline,
  usePaperAccounts,
  type PaperAccountsController,
} from "./usePaperAccounts";
import "./AccountPanel.css";

export interface PendingIntervention {
  kind: PaperCommandKind;
  subAccountId: string;
  roundId?: string;
  instrumentId?: string;
  payload: PaperCommandPayload;
  basedOnLedgerSeq: number;
  title: string;
  detail: string;
  requiresRiskCheck: boolean;
}

function phaseLabel(phase?: string | null): string {
  switch (phase) {
    case "PENDING_ENTRY":
      return t("等待成交", "Awaiting fill");
    case "PARTIALLY_FILLED_PROTECTED":
      return t("部分成交", "Partially filled");
    case "ACTIVE_PROTECTED":
      return t("持仓中", "In position");
    case "EXITING":
      return t("正在平仓", "Exiting");
    case "CLOSED":
      return t("已平仓", "Closed");
    case "CANCELLED":
      return t("已撤单", "Cancelled");
    case "LIQUIDATED":
      return t("已强平", "Liquidated");
    default:
      return phase ?? "";
  }
}

function tradingStateLabel(state: TradingState): string {
  switch (state) {
    case "ACTIVE":
      return t("正常运行 (ACTIVE)", "ACTIVE");
    case "REDUCING":
      return t("只减仓 (REDUCING)", "REDUCING");
    case "HALTED":
      return t("已停机 (HALTED)", "HALTED");
  }
}

function tradingStateClass(state: TradingState): string {
  switch (state) {
    case "ACTIVE":
      return "paper-state-badge paper-state-badge--active";
    case "REDUCING":
      return "paper-state-badge paper-state-badge--reducing";
    case "HALTED":
      return "paper-state-badge paper-state-badge--halted";
  }
}

function commandKindLabel(kind: PaperCommandKind): string {
  switch (kind) {
    case "CLOSE_ALL":
      return t("全平", "Close all");
    case "REDUCE_RATIO":
      return t("按比例减仓", "Reduce position");
    case "MOVE_TO_BREAKEVEN":
      return t("移至保本", "Move to breakeven");
    case "CANCEL_ENTRY":
      return t("撤入场单", "Cancel entry");
    case "TIGHTEN_STOP":
      return t("收紧止损", "Tighten stop");
    case "WIDEN_STOP":
      return t("放宽止损", "Widen stop");
    case "MODIFY_TAKE_PROFIT":
      return t("调整止盈", "Modify take profit");
    case "SET_TRADING_STATE":
      return t("切换交易状态", "Set trading state");
  }
}

function commandReceiptLabel(rec: PaperCommandRecord): { text: string; cls: string } {
  const receipt = rec.receipt;
  if (!receipt) {
    return {
      text: rec.claimed ? t("账户执行中…", "Executing…") : t("待领取…", "Queued…"),
      cls: "paper-command-status--pending",
    };
  }
  if (receipt.accepted) {
    if (receipt.reason === "NOOP") {
      return {
        text: t("无变化 (NOOP)", "No change (NOOP)"),
        cls: "paper-command-status--ok",
      };
    }
    return {
      text: t("已执行", "Executed"),
      cls: "paper-command-status--ok",
    };
  }
  if (receipt.reason === "COMMAND_STALE") {
    return {
      text: t("状态已过期 (COMMAND_STALE)", "Stale (COMMAND_STALE)"),
      cls: "paper-command-status--err",
    };
  }
  return {
    text: t(
      `已拒绝 · ${receipt.reason ?? "未知原因"}`,
      `Rejected · ${receipt.reason ?? "unknown"}`,
    ),
    cls: "paper-command-status--err",
  };
}

function formatTime(ts: number): string {
  return new Intl.DateTimeFormat(getLocale() === "zh" ? "zh-CN" : "en-US", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(ts);
}

function formatPct(raw: string | null | undefined): string {
  if (raw == null) return "—";
  const num = Number(raw);
  if (!Number.isFinite(num)) return raw;
  return `${(num * 100).toFixed(2)}%`;
}

function formatSigned(raw: string | null | undefined): string {
  if (raw == null) return "—";
  const num = Number(raw);
  if (!Number.isFinite(num)) return raw;
  return `${num > 0 ? "+" : ""}${raw}`;
}

function pnlClass(raw: string | null | undefined): string | undefined {
  if (raw == null) return undefined;
  const num = Number(raw);
  if (!Number.isFinite(num) || num === 0) return undefined;
  return num > 0 ? "paper-pnl-pos" : "paper-pnl-neg";
}

function makeCommandId(): string {
  const rand = Math.random().toString(36).slice(2, 8);
  return `cmd-${Date.now()}-${rand}`;
}

function AccountStateSummary({
  state,
  subAccountId,
  ledgerSeq,
  online,
  busy,
  onRequestAction,
}: {
  state: PaperAccountState;
  subAccountId: string;
  ledgerSeq: number;
  online: boolean;
  busy: boolean;
  onRequestAction?: (action: PendingIntervention) => void;
}) {
  const currency = state.currency ?? "USDT";
  const modeLabel =
    state.margin_mode === "ISOLATED"
      ? t("逐仓", "Isolated")
      : state.margin_mode === "CROSS"
        ? t("全仓", "Cross")
        : null;

  const stateRank: Record<TradingState, number> = {
    ACTIVE: 0,
    REDUCING: 1,
    HALTED: 2,
  };

  return (
    <section
      className="paper-summary-card"
      data-testid="paper-account-state"
      aria-label={t("账户资金与风控状态", "Account balance and risk state")}
    >
      <div className="paper-section-header">
        <h3>
          {t("账户权益与风控", "Account & Risk")}
          {modeLabel ? ` · ${modeLabel}` : ""}
          {state.leverage ? ` ${state.leverage}x` : ""}
        </h3>
        <span
          className={tradingStateClass(state.trading_state)}
          data-testid="paper-trading-state"
        >
          {tradingStateLabel(state.trading_state)}
        </span>
      </div>
      {state.trading_state_reason && (
        <p className="paper-row-sub">
          {t("状态原因", "Reason")}: {state.trading_state_reason}
        </p>
      )}
      <dl className="paper-metrics-grid">
        <div>
          <dt>{t("权益", "Equity")}</dt>
          <dd data-testid="paper-equity">
            {state.equity} {currency}
          </dd>
        </div>
        <div>
          <dt>{t("可用资金", "Available cash")}</dt>
          <dd>
            {state.cash_available} {currency}
          </dd>
        </div>
        <div>
          <dt>{t("已用保证金", "Margin used")}</dt>
          <dd>
            {state.margin_used} {currency}
          </dd>
        </div>
        <div>
          <dt>{t("风险敞口 (R)", "Open R")}</dt>
          <dd>{state.open_r} R</dd>
        </div>
        <div>
          <dt>{t("已占用风险", "Risk reserved")}</dt>
          <dd>
            {state.risk_reserved} {currency}
          </dd>
        </div>
        <div>
          <dt>{t("日内回撤", "Intraday DD")}</dt>
          <dd>{formatPct(state.intraday_drawdown_pct)}</dd>
        </div>
        <div>
          <dt>{t("峰值回撤", "Peak DD")}</dt>
          <dd>{formatPct(state.peak_drawdown_pct)}</dd>
        </div>
      </dl>

      {onRequestAction && (
        <div className="paper-state-switcher" data-testid="paper-state-switcher">
          <span className="paper-state-switcher-label">
            {t("切换状态", "State")}:
          </span>
          {(["ACTIVE", "REDUCING", "HALTED"] as const).map((target) => {
            const isCurrent = state.trading_state === target;
            const requiresRiskCheck = stateRank[target] < stateRank[state.trading_state];
            const btnClass = isCurrent
              ? "paper-action-btn paper-action-btn--active-state"
              : target === "HALTED"
                ? "paper-action-btn paper-action-btn--danger"
                : target === "REDUCING"
                  ? "paper-action-btn paper-action-btn--warn"
                  : "paper-action-btn";
            const label =
              target === "ACTIVE"
                ? t("恢复正常", "Active")
                : target === "REDUCING"
                  ? t("只减仓", "Reducing")
                  : t("停机", "Halted");
            return (
              <button
                key={target}
                type="button"
                className={btnClass}
                data-testid={`paper-state-btn-${target}`}
                disabled={!online || busy || isCurrent}
                title={
                  !online
                    ? t("账户离线，操作已禁用", "Account offline — actions disabled")
                    : undefined
                }
                onClick={() =>
                  onRequestAction({
                    kind: "SET_TRADING_STATE",
                    subAccountId,
                    payload: { trading_state: target },
                    basedOnLedgerSeq: ledgerSeq,
                    title: t(`切换交易状态为 ${target}`, `Switch state to ${target}`),
                    detail: t(
                      `子账户 ${subAccountId}：${state.trading_state} → ${target}`,
                      `Sub-account ${subAccountId}: ${state.trading_state} → ${target}`,
                    ),
                    requiresRiskCheck,
                  })
                }
              >
                {label}
              </button>
            );
          })}
        </div>
      )}
    </section>
  );
}

function RoundRows({
  rows,
  empty,
  testIdPrefix,
  subAccountId,
  ledgerSeq,
  online,
  busy,
  showPositionActions = false,
  showPendingActions = false,
  onRequestAction,
}: {
  rows: PaperRound[];
  empty: string;
  testIdPrefix: string;
  subAccountId?: string;
  ledgerSeq?: number;
  online?: boolean;
  busy?: boolean;
  showPositionActions?: boolean;
  showPendingActions?: boolean;
  onRequestAction?: (action: PendingIntervention) => void;
}) {
  if (rows.length === 0) {
    return (
      <p className="panel-empty" data-testid={`${testIdPrefix}-empty`}>
        {empty}
      </p>
    );
  }
  const disabledTip = !online
    ? t("账户离线，操作已禁用", "Account offline — actions disabled")
    : undefined;

  return (
    <ul className="paper-rows-list" data-testid={`${testIdPrefix}-list`}>
      {rows.map((row) => {
        const qtyNum = Number(row.quantity);
        const isOpen = Number.isFinite(qtyNum) && qtyNum > 0;
        const hasPendingEntry =
          row.phase === "PENDING_ENTRY" ||
          row.phase === "PARTIALLY_FILLED_PROTECTED" ||
          (row.entry_low != null && row.entry_high != null);
        const renderOpenBtns = showPositionActions && isOpen;
        const renderCancelBtn = showPendingActions && hasPendingEntry;

        return (
          <li key={row.round_id} className="paper-row-item">
            <div className="paper-row-head">
              <strong className="paper-row-instrument">{row.instrument_id}</strong>
              <span className="paper-section-count">{phaseLabel(row.phase)}</span>
            </div>
            <p className="paper-row-sub">{row.round_id}</p>
            <dl className="paper-metrics-grid">
              <div>
                <dt>{t("方向", "Direction")}</dt>
                <dd>{row.direction === "LONG" ? t("做多", "Long") : t("做空", "Short")}</dd>
              </div>
              <div>
                <dt>{t("剩余数量", "Remaining qty")}</dt>
                <dd>{row.quantity}</dd>
              </div>
              {row.entry_price != null && (
                <div>
                  <dt>{t("均价", "Entry price")}</dt>
                  <dd>{row.entry_price}</dd>
                </div>
              )}
              {row.entry_low != null && row.entry_high != null && (
                <div>
                  <dt>{t("入场带", "Entry band")}</dt>
                  <dd>
                    {row.entry_low}–{row.entry_high}
                  </dd>
                </div>
              )}
              {row.mark_price != null && (
                <div>
                  <dt>{t("标记价", "Mark price")}</dt>
                  <dd>{row.mark_price}</dd>
                </div>
              )}
              {row.unrealized_pnl != null && (
                <div>
                  <dt>{t("浮动盈亏", "Unrealized PnL")}</dt>
                  <dd className={pnlClass(row.unrealized_pnl)}>
                    {formatSigned(row.unrealized_pnl)}
                  </dd>
                </div>
              )}
              <div>
                <dt>{t("止损", "Stop loss")}</dt>
                <dd>{row.stop}</dd>
              </div>
              {(row.take_profit?.length ?? 0) > 0 && (
                <div>
                  <dt>{t("止盈", "Take profit")}</dt>
                  <dd>{row.take_profit?.join(" / ")}</dd>
                </div>
              )}
            </dl>

            {onRequestAction && subAccountId && (renderOpenBtns || renderCancelBtn) && (
              <div
                className="paper-action-row"
                data-testid={`${testIdPrefix}-actions-${row.round_id}`}
              >
                {renderOpenBtns && (
                  <>
                    <button
                      type="button"
                      className="paper-action-btn paper-action-btn--danger"
                      data-testid={`paper-btn-close-${row.round_id}`}
                      disabled={!online || busy}
                      title={disabledTip}
                      onClick={() =>
                        onRequestAction({
                          kind: "CLOSE_ALL",
                          subAccountId,
                          roundId: row.round_id,
                          instrumentId: row.instrument_id,
                          payload: {},
                          basedOnLedgerSeq: ledgerSeq ?? 0,
                          title: t("确认全平持仓", "Confirm Close All"),
                          detail: t(
                            `${row.instrument_id} (${row.round_id})，当前数量 ${row.quantity}`,
                            `${row.instrument_id} (${row.round_id}), qty ${row.quantity}`,
                          ),
                          requiresRiskCheck: false,
                        })
                      }
                    >
                      {t("全平", "Close all")}
                    </button>
                    <button
                      type="button"
                      className="paper-action-btn paper-action-btn--warn"
                      data-testid={`paper-btn-reduce-${row.round_id}`}
                      disabled={!online || busy}
                      title={disabledTip}
                      onClick={() =>
                        onRequestAction({
                          kind: "REDUCE_RATIO",
                          subAccountId,
                          roundId: row.round_id,
                          instrumentId: row.instrument_id,
                          payload: { ratio: "0.5" },
                          basedOnLedgerSeq: ledgerSeq ?? 0,
                          title: t("确认减仓 50%", "Confirm Reduce 50%"),
                          detail: t(
                            `${row.instrument_id} (${row.round_id})，按 50% 减仓`,
                            `${row.instrument_id} (${row.round_id}), reduce by 50%`,
                          ),
                          requiresRiskCheck: false,
                        })
                      }
                    >
                      {t("减半", "Reduce 50%")}
                    </button>
                    <button
                      type="button"
                      className="paper-action-btn"
                      data-testid={`paper-btn-breakeven-${row.round_id}`}
                      disabled={!online || busy}
                      title={disabledTip}
                      onClick={() =>
                        onRequestAction({
                          kind: "MOVE_TO_BREAKEVEN",
                          subAccountId,
                          roundId: row.round_id,
                          instrumentId: row.instrument_id,
                          payload: {},
                          basedOnLedgerSeq: ledgerSeq ?? 0,
                          title: t("确认止损移至保本", "Confirm Move Stop to Breakeven"),
                          detail: t(
                            `${row.instrument_id} (${row.round_id})，止损移至持仓均价 ${row.entry_price ?? "—"}`,
                            `${row.instrument_id} (${row.round_id}), move stop to entry ${row.entry_price ?? "—"}`,
                          ),
                          requiresRiskCheck: false,
                        })
                      }
                    >
                      {t("移至保本", "Move to BE")}
                    </button>
                  </>
                )}
                {renderCancelBtn && (
                  <button
                    type="button"
                    className="paper-action-btn"
                    data-testid={`paper-btn-cancel-entry-${row.round_id}`}
                    disabled={!online || busy}
                    title={disabledTip}
                    onClick={() =>
                      onRequestAction({
                        kind: "CANCEL_ENTRY",
                        subAccountId,
                        roundId: row.round_id,
                        instrumentId: row.instrument_id,
                        payload: {},
                        basedOnLedgerSeq: ledgerSeq ?? 0,
                        title: t("确认撤回入场单", "Confirm Cancel Entry"),
                        detail: t(
                          `${row.instrument_id} (${row.round_id}) 撤回未成交入场单`,
                          `${row.instrument_id} (${row.round_id}) cancel resting entry`,
                        ),
                        requiresRiskCheck: false,
                      })
                    }
                  >
                    {t("撤入场单", "Cancel entry")}
                  </button>
                )}
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

export function LedgerSections({
  view,
  online = false,
  busy = false,
  onRequestAction,
}: {
  view: PaperAccountView;
  online?: boolean;
  busy?: boolean;
  onRequestAction?: (action: PendingIntervention) => void;
}) {
  useLocale();
  const ledgerSeq = view.ledger_seq ?? 0;

  return (
    <div className="paper-ledger-sections" data-testid="paper-ledger-sections">
      {view.account_state && (
        <AccountStateSummary
          state={view.account_state}
          subAccountId={view.sub_account_id}
          ledgerSeq={ledgerSeq}
          online={online}
          busy={busy}
          onRequestAction={onRequestAction}
        />
      )}

      <section className="paper-section-card" data-testid="paper-positions-section">
        <div className="paper-section-header">
          <h3>{t("持仓", "Positions")}</h3>
          <span className="paper-section-count">{view.positions.length}</span>
        </div>
        <RoundRows
          rows={view.positions}
          empty={t("当前无持仓", "No open positions")}
          testIdPrefix="paper-positions"
          subAccountId={view.sub_account_id}
          ledgerSeq={ledgerSeq}
          online={online}
          busy={busy}
          showPositionActions={Boolean(onRequestAction)}
          showPendingActions={Boolean(onRequestAction)}
          onRequestAction={onRequestAction}
        />
      </section>

      <section className="paper-section-card" data-testid="paper-rounds-section">
        <div className="paper-section-header">
          <h3>{t("仓位回合", "Position rounds")}</h3>
          <span className="paper-section-count">{view.rounds.length}</span>
        </div>
        <RoundRows
          rows={view.rounds}
          empty={t("暂无仓位回合", "No position rounds yet")}
          testIdPrefix="paper-rounds"
          subAccountId={view.sub_account_id}
          ledgerSeq={ledgerSeq}
          online={online}
          busy={busy}
          showPositionActions={false}
          showPendingActions={Boolean(onRequestAction)}
          onRequestAction={onRequestAction}
        />
      </section>

      <section className="paper-section-card" data-testid="paper-fills-section">
        <div className="paper-section-header">
          <h3>{t("成交", "Fills")}</h3>
          <span className="paper-section-count">{view.fills.length}</span>
        </div>
        {view.fills.length === 0 ? (
          <p className="panel-empty" data-testid="paper-fills-empty">
            {t("暂无成交", "No fills yet")}
          </p>
        ) : (
          <ul className="paper-rows-list" data-testid="paper-fills-list">
            {view.fills.map((row) => (
              <li key={row.event_id} className="paper-row-item">
                <div className="paper-row-head">
                  <strong className="paper-row-instrument">{row.instrument_id}</strong>
                  <span className="paper-section-count">
                    {row.kind === "ENTRY_FILLED" ? t("入场", "Entry") : t("出场", "Exit")}
                    {row.reason ? ` · ${row.reason}` : ""}
                  </span>
                </div>
                <p className="paper-row-sub">
                  <time dateTime={new Date(row.ts_event).toISOString()}>
                    {formatTime(row.ts_event)}
                  </time>
                  {" · "}
                  {row.round_id}
                </p>
                <dl className="paper-metrics-grid">
                  <div>
                    <dt>{t("成交价", "Fill price")}</dt>
                    <dd>{row.price}</dd>
                  </div>
                  <div>
                    <dt>{t("数量", "Quantity")}</dt>
                    <dd>{row.quantity}</dd>
                  </div>
                  {row.fee != null && (
                    <div>
                      <dt>{t("手续费", "Fee")}</dt>
                      <dd>{row.fee}</dd>
                    </div>
                  )}
                  {row.realized_pnl != null && (
                    <div>
                      <dt>{t("已实现盈亏", "Realized PnL")}</dt>
                      <dd className={pnlClass(row.realized_pnl)}>
                        {formatSigned(row.realized_pnl)}
                      </dd>
                    </div>
                  )}
                </dl>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

export interface AccountPanelProps {
  controller?: PaperAccountsController;
}

function AccountPanelView({ controller }: { controller: PaperAccountsController }) {
  useLocale();
  const {
    accounts,
    views,
    selectedAccount,
    selectedView,
    selectAccount,
    loading,
    error,
    refresh,
  } = controller;

  const online = isPaperProcessOnline(selectedView);
  const process = selectedView?.process ?? null;
  const feed = selectedView?.feed_status ?? null;

  const [pendingAction, setPendingAction] = useState<PendingIntervention | null>(null);
  const [reasonText, setReasonText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [commandError, setCommandError] = useState<string | null>(null);
  const [commands, setCommands] = useState<PaperCommandRecord[]>([]);

  const loadCommands = useCallback(async (accountId: string) => {
    if (!accountId) {
      setCommands([]);
      return;
    }
    try {
      const list = await api.paperCommands(accountId);
      setCommands(Array.isArray(list) ? list.slice(0, 5) : []);
    } catch {
      // Non-fatal if command history endpoint is temporarily unreachable.
    }
  }, []);

  useEffect(() => {
    void loadCommands(selectedAccount);
  }, [selectedAccount, loadCommands]);

  const hasPendingCommand = commands.some((c) => c.receipt == null);
  useEffect(() => {
    if (!selectedAccount || !hasPendingCommand) return;
    let active = true;
    const timer = window.setInterval(() => {
      void api
        .paperCommands(selectedAccount)
        .then((list) => {
          if (!active || !Array.isArray(list)) return;
          const sliced = list.slice(0, 5);
          setCommands(sliced);
          if (!sliced.some((c) => c.receipt == null)) {
            void refresh();
          }
        })
        .catch(() => undefined);
    }, 400);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [selectedAccount, hasPendingCommand, refresh]);

  const handleConfirmAction = async () => {
    if (!pendingAction || submitting) return;
    setSubmitting(true);
    setCommandError(null);
    const trimmedReason = reasonText.trim();
    const command: PaperCommand = {
      command_id: makeCommandId(),
      sub_account_id: pendingAction.subAccountId,
      ...(pendingAction.roundId ? { round_id: pendingAction.roundId } : {}),
      kind: pendingAction.kind,
      payload: {
        ...pendingAction.payload,
        ...(trimmedReason ? { reason: trimmedReason } : {}),
      },
      based_on_ledger_seq: pendingAction.basedOnLedgerSeq,
      issued_by: "trader",
      issued_ts: Date.now(),
    };
    try {
      const created = await api.submitPaperCommand(command);
      setCommands((prev) => [created, ...prev.filter((c) => c.command.command_id !== created.command.command_id)].slice(0, 5));
      setPendingAction(null);
      setReasonText("");
    } catch (caught) {
      setCommandError(
        caught instanceof Error
          ? caught.message
          : t("干预命令提交失败，请重试。", "Failed to submit intervention command."),
      );
      setPendingAction(null);
      setReasonText("");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section
      className="dock-panel paper-account-panel"
      data-testid="paper-account-panel"
      aria-label={t("模拟盘账户", "Paper Account")}
    >
      <header className="dock-panel-head">
        <div>
          <h2>{t("模拟盘账户", "Paper Account")}</h2>
          {selectedView && (
            <span className="dock-panel-meta">
              {t("账本序号", "Ledger")} #{selectedView.ledger_seq ?? 0}
            </span>
          )}
        </div>
        <button
          type="button"
          className="ghost-button"
          onClick={() => {
            void refresh();
            void loadCommands(selectedAccount);
          }}
        >
          {t("刷新", "Refresh")}
        </button>
      </header>

      <PaperProcessControl
        views={views}
        selectedView={selectedView}
        onRefreshAccounts={refresh}
      />

      {accounts.length > 0 && (
        <div className="paper-account-selector-row">
          <label className="paper-account-select-label">
            <span>{t("子账户", "Sub-account")}</span>
            <select
              className="paper-account-select"
              data-testid="paper-account-select"
              value={selectedAccount}
              onChange={(event) => selectAccount(event.target.value)}
            >
              {accounts.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </label>
          {selectedView && (
            <span
              className={`paper-status-badge ${
                online ? "paper-status-badge--online" : "paper-status-badge--offline"
              }`}
              data-testid="paper-process-badge"
            >
              {online
                ? t("在线", "Online")
                : process?.stopped_ts != null
                  ? t("离线 · 已停止", "Offline · Stopped")
                  : t("离线", "Offline")}
            </span>
          )}
        </div>
      )}

      {process && (
        <div className="paper-process-meta" data-testid="paper-process-meta">
          <span>
            {t("方案", "Scheme")}: {process.scheme}
          </span>
          <span>PID: {process.pid}</span>
          <span>
            {t("心跳", "Heartbeat")}: {formatTime(process.heartbeat_ts)}
          </span>
        </div>
      )}

      {feed && (feed.service_warning || feed.reduce_reason || !feed.healthy) && (
        <div className="paper-warning-banner" role="alert" data-testid="paper-feed-warning">
          {feed.service_warning
            ? `${t("服务告警", "Service warning")}: ${feed.service_warning}`
            : feed.reduce_reason
              ? `${t("行情降级", "Feed degraded")}: ${feed.reduce_reason}`
              : t(
                  "账户进程离线或行情未就绪，当前展示最后一次只读投影。",
                  "Account process offline or feed not ready; showing last read-only projection.",
                )}
        </div>
      )}

      {commandError && (
        <p className="panel-error" role="alert" data-testid="paper-command-error">
          {commandError}
        </p>
      )}

      {commands.length > 0 && (
        <div className="paper-command-feedback" data-testid="paper-command-feedback">
          {commands.slice(0, 3).map((rec) => {
            const status = commandReceiptLabel(rec);
            return (
              <div key={rec.command.command_id} className="paper-command-item">
                <span>
                  {commandKindLabel(rec.command.kind)}
                  {rec.command.round_id ? ` · ${rec.command.round_id}` : ""}
                  {rec.command.payload?.trading_state
                    ? ` → ${rec.command.payload.trading_state}`
                    : ""}
                </span>
                <span className={status.cls}>{status.text}</span>
              </div>
            );
          })}
        </div>
      )}

      {error && (
        <p className="panel-error" role="alert">
          {error}{" "}
          <button type="button" className="ghost-button" onClick={() => void refresh()}>
            {t("重试", "Retry")}
          </button>
        </p>
      )}

      {loading && accounts.length === 0 ? (
        <p className="panel-empty" role="status">
          {t("正在读取模拟盘账户投影…", "Loading paper account projections…")}
        </p>
      ) : accounts.length === 0 ? (
        <div className="paper-section-card" data-testid="paper-accounts-empty">
          <p className="panel-empty">
            {t(
              "暂未发现模拟盘账户投影。启动本地账户进程后将自动实时同步权益、持仓与成交。",
              "No paper account projections found yet. Start a local account process to stream balances, positions, and fills.",
            )}
          </p>
          <p className="paper-cli-hint">
            talaria account run &lt;scheme.yaml&gt; --mode paper
          </p>
        </div>
      ) : selectedView ? (
        <LedgerSections
          view={selectedView}
          online={online}
          busy={submitting}
          onRequestAction={(action) => {
            setCommandError(null);
            setReasonText("");
            setPendingAction(action);
          }}
        />
      ) : (
        <p className="panel-empty" role="status">
          {t("正在读取子账户投影…", "Loading account projection…")}
        </p>
      )}

      {pendingAction && (
        <div
          className="paper-confirm-backdrop"
          role="dialog"
          aria-modal="true"
          aria-label={pendingAction.title}
          data-testid="paper-confirm-dialog"
        >
          <div className="paper-confirm-dialog">
            <h3>{pendingAction.title}</h3>
            <p>{pendingAction.detail}</p>
            <p>
              {pendingAction.requiresRiskCheck
                ? t(
                    "该操作将经过风控插槽校验（ADR-0015 底线 2）。",
                    "This action requires risk slot approval (ADR-0015 baseline 2).",
                  )
                : t(
                    "只减风险操作：直达成交插槽执行（ADR-0015 底线 2）。",
                    "Risk-reducing action: routes directly to execution seam (ADR-0015 baseline 2).",
                  )}
            </p>
            <label className="paper-confirm-reason">
              <span>{t("备注原因（可选）", "Reason (optional)")}</span>
              <input
                type="text"
                maxLength={256}
                value={reasonText}
                onChange={(e) => setReasonText(e.target.value)}
                placeholder={t("例如：手动止盈离场", "e.g. manual exit")}
              />
            </label>
            <div className="paper-confirm-actions">
              <button
                type="button"
                className="ghost-button"
                data-testid="paper-confirm-cancel"
                disabled={submitting}
                onClick={() => setPendingAction(null)}
              >
                {t("取消", "Cancel")}
              </button>
              <button
                type="button"
                className="paper-action-btn paper-action-btn--danger"
                data-testid="paper-confirm-submit"
                disabled={submitting}
                onClick={() => void handleConfirmAction()}
              >
                {submitting ? t("提交中…", "Submitting…") : t("确认执行", "Confirm")}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

export function AccountPanel({ controller }: AccountPanelProps) {
  const internal = usePaperAccounts();
  return <AccountPanelView controller={controller ?? internal} />;
}
