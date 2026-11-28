import { useMemo, useState } from "react";
import { ChevronDown } from "lucide-react";
import type { RunRoundRecord } from "@talaria/contracts";

import { timeframeToMs } from "../../lib/chartData";
import { t, useLocale } from "../../lib/i18n";
import {
  buildRunPluginPairs,
  groupEquitySeries,
  replayStartBeforeEntry,
  type BookedEquitySeries,
} from "./model";
import type { RunsController } from "./useRuns";

export interface RunsPanelProps {
  controller: RunsController;
  activeTimeframe: string;
  onJumpToRound: (round: RunRoundRecord, timeframe?: string) => void;
  onReplayBeforeRound: (params: {
    instrumentId: string;
    timeframe?: string;
    fromTs: number;
  }) => void;
  layout?: "vertical" | "wide";
  onCollapse?: () => void;
}

function formatNumber(value: string | number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "—";
  const num = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(num)) return "—";
  return num.toFixed(digits);
}

function formatSignedDelta(value: number | null, digits = 2): string {
  if (value === null || !Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}`;
}

function formatTs(ts: number | null | undefined): string {
  if (ts === null || ts === undefined || !Number.isFinite(ts)) return "—";
  return new Date(ts).toISOString().replace("T", " ").slice(0, 16);
}

function EquitySparkline({ series }: { series: BookedEquitySeries }) {
  if (series.points.length === 0) return null;
  const width = 260;
  const height = 56;
  const values = series.points.map((p) => p.equity);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const coords = series.points
    .map((pt, idx) => {
      const x =
        series.points.length === 1
          ? width / 2
          : (idx / (series.points.length - 1)) * (width - 8) + 4;
      const y = height - 6 - ((pt.equity - min) / span) * (height - 12);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  return (
    <svg
      className="runs-equity-svg"
      viewBox={`0 0 ${width} ${height}`}
      width="100%"
      height={height}
      aria-label={t("已实现净值曲线", "Booked equity curve")}
    >
      <polyline
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
        points={coords}
      />
    </svg>
  );
}

export function RunsPanel({
  controller,
  activeTimeframe,
  onJumpToRound,
  onReplayBeforeRound,
  layout = "vertical",
  onCollapse,
}: RunsPanelProps) {
  useLocale();
  const [lookbackBars, setLookbackBars] = useState(20);
  const {
    runs,
    selectedRunId,
    selectedRun,
    rounds,
    equityPoints,
    selectedRoundId,
    loadingRuns,
    loadingDetail,
    error,
    refresh,
    selectRun,
    selectRound,
  } = controller;

  const pairs = useMemo(() => buildRunPluginPairs(runs), [runs]);
  const equitySeries = useMemo(() => groupEquitySeries(equityPoints), [equityPoints]);

  const effectiveTf = selectedRun?.timeframe ?? activeTimeframe;
  const tfMs = timeframeToMs(effectiveTf);

  const listContent = (
    <>
      {pairs.length > 0 && (
        <div className="runs-delta-section" data-testid="runs-plugin-deltas">
          <h3>{t("l1_trade_sim vs bar_sim_1m 差值", "l1_trade_sim vs bar_sim_1m Delta")}</h3>
          <ul className="entity-list">
            {pairs.map((pair) => (
              <li key={pair.pairKey} className="entity-card">
                <div className="entity-card-top">
                  <strong>{pair.scheme}</strong>
                  <span>
                    {pair.instrumentLabel} · {pair.timeframe ?? "—"}
                  </span>
                </div>
                {pair.deltas.map((d) => (
                  <div
                    key={`${d.sub_account_id}:${d.currency}`}
                    className="runs-delta-row"
                    data-testid={`runs-delta-${pair.scheme}-${d.currency}`}
                  >
                    <span>
                      {d.sub_account_id} ({d.currency})
                    </span>
                    <span>
                      L1: {formatNumber(d.l1NetPnl)} / 1m: {formatNumber(d.barSimNetPnl)} (Δ{" "}
                      {formatSignedDelta(d.netPnlDelta)})
                    </span>
                    <span>
                      DD Δ {formatSignedDelta(d.maxDrawdownDelta)} ·{" "}
                      {t("回合", "Rounds")} Δ {formatSignedDelta(d.roundCountDelta, 0)}
                    </span>
                  </div>
                ))}
              </li>
            ))}
          </ul>
        </div>
      )}

      {loadingRuns && runs.length === 0 ? (
        <p className="panel-empty">{t("加载回测记录中…", "Loading runs…")}</p>
      ) : runs.length === 0 ? (
        <p className="panel-empty" data-testid="runs-empty">
          {t("暂无已导出的回测结果。", "No exported runs found.")}
        </p>
      ) : (
        <ul className="entity-list" data-testid="runs-list">
          {runs.map((run) => {
            const isSelected = run.run_id === selectedRunId;
            const primaryAccount = run.accounts?.[0];
            return (
              <li
                key={run.run_id}
                className={`entity-card${isSelected ? " is-highlighted" : ""}`}
                data-testid={`run-item-${run.run_id}`}
              >
                <button
                  type="button"
                  className="drawing-select-button"
                  onClick={() => selectRun(run.run_id)}
                >
                  <div className="entity-card-top">
                    <strong>{run.scheme}</strong>
                    <span className="status-pill">{run.fill_plugin}</span>
                  </div>
                  <div className="entity-card-main">
                    <span>
                      {run.instrument_id ?? run.instruments?.join(",") ?? "—"} ·{" "}
                      {run.timeframe ?? "—"}
                    </span>
                    {primaryAccount && (
                      <span>
                        {t("净收益", "Net")}: {primaryAccount.net_pnl}{" "}
                        {primaryAccount.currency} · {t("回撤", "MaxDD")}:{" "}
                        {primaryAccount.max_drawdown} · {primaryAccount.round_count}{" "}
                        {t("回合", "rounds")}
                      </span>
                    )}
                  </div>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );

  const detailContent = selectedRun ? (
    <div className="runs-detail" data-testid="runs-detail">
      <div className="runs-equity-section" data-testid="runs-equity-section">
        <div className="entity-card-top">
          <strong>{t("净值与回撤（已实现口径）", "Equity & Drawdown (Booked)")}</strong>
          {loadingDetail && <span>{t("加载中…", "Loading…")}</span>}
        </div>
        {equitySeries.length === 0 ? (
          <p className="panel-empty">{t("无净值分录", "No equity points")}</p>
        ) : (
          equitySeries.map((series) => (
            <div
              key={`${series.sub_account_id}:${series.currency}`}
              className="entity-card"
            >
              <div className="entity-card-top">
                <span>
                  {series.sub_account_id} ({series.currency})
                </span>
                <span>
                  {t("净值", "Eq")}: {formatNumber(series.latestEquity)} ·{" "}
                  {t("最大回撤", "MaxDD")}: {formatNumber(series.maxDrawdown)}
                </span>
              </div>
              <EquitySparkline series={series} />
            </div>
          ))
        )}
      </div>

      <div className="runs-rounds-section" data-testid="runs-rounds-section">
        <div className="entity-card-top">
          <strong>
            {t("Position Rounds", "Position Rounds")} ({rounds.length})
          </strong>
          <label className="runs-lookback-label">
            <span>N=</span>
            <select
              aria-label={t("开仓前柱数", "Lookback bars before entry")}
              data-testid="runs-replay-lookback"
              value={String(lookbackBars)}
              onChange={(e) => setLookbackBars(Number(e.target.value))}
            >
              <option value="10">10</option>
              <option value="20">20</option>
              <option value="50">50</option>
            </select>
          </label>
        </div>

        {rounds.length === 0 ? (
          <p className="panel-empty">{t("无回合记录", "No rounds")}</p>
        ) : (
          <ul className="entity-list">
            {rounds.map((round) => {
              const selected = round.round_id === selectedRoundId;
              return (
                <li
                  key={round.round_id}
                  className={`entity-card${selected ? " is-highlighted" : ""}`}
                  data-testid={`run-round-${round.round_id}`}
                >
                  <button
                    type="button"
                    className="drawing-select-button"
                    onClick={() => {
                      selectRound(round.round_id);
                      onJumpToRound(round, selectedRun.timeframe ?? undefined);
                    }}
                  >
                    <div className="entity-card-top">
                      <span className="entity-tag">
                        {round.direction} · {round.exit_reason ?? round.phase}
                      </span>
                      <span className="status-pill">
                        {t("净盈亏", "PnL")}: {round.net_pnl}
                      </span>
                    </div>
                    <div className="entity-card-main">
                      <span>
                        {formatTs(round.opened_ts)} → {formatTs(round.closed_ts)}
                      </span>
                      <span>
                        {t("开/平", "Entry/Exit")}: {round.entry_price ?? "—"} /{" "}
                        {round.exit_price ?? "—"}
                      </span>
                      <span data-testid={`run-round-mae-mfe-${round.round_id}`}>
                        MAE: {round.mae ?? "—"} · MFE: {round.mfe ?? "—"}
                      </span>
                      <span>
                        SL: {round.stop} · TP:{" "}
                        {round.take_profit && round.take_profit.length > 0
                          ? round.take_profit.join(", ")
                          : "—"}
                      </span>
                    </div>
                  </button>

                  {round.opened_ts !== null && round.opened_ts !== undefined && (
                    <div className="entity-actions">
                      <button
                        type="button"
                        className="ghost-button"
                        data-testid={`run-round-replay-${round.round_id}`}
                        onClick={() => {
                          selectRound(round.round_id);
                          onReplayBeforeRound({
                            instrumentId: round.instrument_id,
                            timeframe: selectedRun.timeframe ?? undefined,
                            fromTs: replayStartBeforeEntry(
                              round.opened_ts!,
                              lookbackBars,
                              tfMs,
                            ),
                          });
                        }}
                      >
                        {t(
                          `从开仓前 ${lookbackBars} 根开始回放`,
                          `Replay ${lookbackBars} bars before entry`,
                        )}
                      </button>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  ) : layout === "wide" && runs.length > 0 ? (
    <p className="panel-empty">
      {t(
        "从左侧选择一条回测记录以查看净值曲线与回合明细。",
        "Select a run on the left to inspect equity curves and position rounds.",
      )}
    </p>
  ) : null;

  return (
    <section
      className={`dock-panel runs-panel${layout === "wide" ? " runs-panel--wide" : ""}`}
      data-testid="runs-panel"
      aria-label={t("回测复盘", "Backtest Runs")}
    >
      <header className="dock-panel-head">
        <div>
          <h2>{t("回测复盘", "Runs")}</h2>
          <span className="dock-panel-meta" data-testid="runs-basis-badge">
            {t("已实现口径", "Booked PnL")} · {runs.length}
          </span>
        </div>
        <div className="wb-inline-actions">
          <button type="button" className="ghost-button" onClick={() => void refresh()}>
            {t("刷新", "Refresh")}
          </button>
          {onCollapse && (
            <button
              type="button"
              className="icon-button"
              data-testid="backtest-drawer-collapse"
              title={t("收起回测面板", "Collapse backtest drawer")}
              aria-label={t("收起回测面板", "Collapse backtest drawer")}
              onClick={onCollapse}
            >
              <ChevronDown size={14} />
            </button>
          )}
        </div>
      </header>

      {error && (
        <p className="panel-error" role="alert">
          {error}
        </p>
      )}

      {layout === "wide" ? (
        <div className="runs-wide-grid">
          <div className="runs-wide-left">{listContent}</div>
          <div className="runs-wide-right">{detailContent}</div>
        </div>
      ) : (
        <>
          {listContent}
          {detailContent}
        </>
      )}
    </section>
  );
}
