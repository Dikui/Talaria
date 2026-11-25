import type { TradeExecution } from "@luxalgo/vela";
import type {
  RunAccountCurrencySummary,
  RunEquityPoint,
  RunExportSummary,
  RunRoundRecord,
} from "@talaria/contracts";

import type { ExecutionBarrierItem } from "../../chart/adapter";

export interface RunPluginPair {
  pairKey: string;
  scheme: string;
  instrumentLabel: string;
  timeframe: string | null;
  l1Run: RunExportSummary | null;
  barSimRun: RunExportSummary | null;
  deltas: RunPairCurrencyDelta[];
}

export interface RunPairCurrencyDelta {
  sub_account_id: string;
  currency: string;
  l1NetPnl: number | null;
  barSimNetPnl: number | null;
  netPnlDelta: number | null;
  l1MaxDrawdown: number | null;
  barSimMaxDrawdown: number | null;
  maxDrawdownDelta: number | null;
  l1RoundCount: number | null;
  barSimRoundCount: number | null;
  roundCountDelta: number | null;
}

function toFinite(val: string | number | null | undefined): number | null {
  if (val === null || val === undefined) return null;
  const num = typeof val === "number" ? val : Number(val);
  return Number.isFinite(num) ? num : null;
}

function pairInstrumentLabel(run: RunExportSummary): string {
  if (run.instrument_id) return run.instrument_id;
  if (Array.isArray(run.instruments) && run.instruments.length > 0) {
    return run.instruments.join(",");
  }
  return "—";
}

function accountKey(acct: Pick<RunAccountCurrencySummary, "sub_account_id" | "currency">): string {
  return `${acct.sub_account_id}:${acct.currency}`;
}

/**
 * Pair `l1_trade_sim` and `bar_sim_1m` runs that share scheme, instruments,
 * timeframe and window so the Runs panel can display side-by-side deltas.
 */
export function buildRunPluginPairs(runs: readonly RunExportSummary[]): RunPluginPair[] {
  const groups = new Map<
    string,
    {
      scheme: string;
      instrumentLabel: string;
      timeframe: string | null;
      l1Run: RunExportSummary | null;
      barSimRun: RunExportSummary | null;
    }
  >();

  for (const run of runs) {
    if (run.fill_plugin !== "l1_trade_sim" && run.fill_plugin !== "bar_sim_1m") {
      continue;
    }
    const instLabel = pairInstrumentLabel(run);
    const key = [
      run.scheme,
      instLabel,
      run.timeframe ?? "",
      run.start ?? "",
      run.end ?? "",
    ].join("|");
    const entry = groups.get(key) ?? {
      scheme: run.scheme,
      instrumentLabel: instLabel,
      timeframe: run.timeframe ?? null,
      l1Run: null,
      barSimRun: null,
    };
    if (run.fill_plugin === "l1_trade_sim" && !entry.l1Run) {
      entry.l1Run = run;
    } else if (run.fill_plugin === "bar_sim_1m" && !entry.barSimRun) {
      entry.barSimRun = run;
    }
    groups.set(key, entry);
  }

  const pairs: RunPluginPair[] = [];
  for (const [pairKey, entry] of groups.entries()) {
    if (!entry.l1Run || !entry.barSimRun) continue;
    const l1Accounts = new Map<string, RunAccountCurrencySummary>();
    for (const acct of entry.l1Run.accounts ?? []) {
      l1Accounts.set(accountKey(acct), acct);
    }
    const barAccounts = new Map<string, RunAccountCurrencySummary>();
    for (const acct of entry.barSimRun.accounts ?? []) {
      barAccounts.set(accountKey(acct), acct);
    }
    const allKeys = new Set([...l1Accounts.keys(), ...barAccounts.keys()]);
    const deltas: RunPairCurrencyDelta[] = [];
    for (const key of allKeys) {
      const l1 = l1Accounts.get(key);
      const bar = barAccounts.get(key);
      const l1Net = toFinite(l1?.net_pnl);
      const barNet = toFinite(bar?.net_pnl);
      const l1Dd = toFinite(l1?.max_drawdown);
      const barDd = toFinite(bar?.max_drawdown);
      deltas.push({
        sub_account_id: l1?.sub_account_id ?? bar?.sub_account_id ?? "default",
        currency: l1?.currency ?? bar?.currency ?? "USDT",
        l1NetPnl: l1Net,
        barSimNetPnl: barNet,
        netPnlDelta: l1Net !== null && barNet !== null ? l1Net - barNet : null,
        l1MaxDrawdown: l1Dd,
        barSimMaxDrawdown: barDd,
        maxDrawdownDelta: l1Dd !== null && barDd !== null ? l1Dd - barDd : null,
        l1RoundCount: l1?.round_count ?? null,
        barSimRoundCount: bar?.round_count ?? null,
        roundCountDelta:
          l1 && bar ? l1.round_count - bar.round_count : null,
      });
    }
    pairs.push({ pairKey, ...entry, deltas });
  }
  return pairs;
}

/**
 * Map `RunRoundRecord[]` fills to Vela `TradeExecution[]` anchored to bar open slots
 * (`Math.floor(ts_event / tf_ms) * tf_ms`), preserving original order so Vela
 * stacks intra-bar fills natively.
 */
export function roundsToTradeExecutions(
  rounds: readonly RunRoundRecord[],
  tfMs: number,
  instrumentId?: string,
): TradeExecution[] {
  if (!Array.isArray(rounds) || tfMs <= 0) return [];
  const raw: Array<{ seq: number; tsEvent: number; marker: TradeExecution }> = [];
  let seq = 0;
  for (const round of rounds) {
    if (instrumentId && round.instrument_id !== instrumentId) continue;
    for (const fill of round.fills ?? []) {
      const price = Number(fill.price);
      const qty = Number(fill.quantity);
      if (!Number.isFinite(price)) continue;
      const marker: TradeExecution = {
        time: Math.floor(fill.ts_event / tfMs) * tfMs,
        price,
        side: fill.side,
        kind: fill.kind === "ENTRY_FILLED" ? "entry" : "exit",
        tradeId: round.round_id,
        label: fill.reason,
        ...(Number.isFinite(qty) ? { qty } : {}),
      };
      raw.push({ seq, tsEvent: fill.ts_event, marker });
      seq += 1;
    }
  }
  raw.sort((a, b) => a.tsEvent - b.tsEvent || a.seq - b.seq);
  return raw.map((item) => item.marker);
}

/**
 * Map a selected `Position Round` to the workbench's shared `ExecutionBarrierItem`
 * rendered by `LAYER_EXECUTION_BARRIER`.
 */
export function runRoundToBarrier(round: RunRoundRecord): ExecutionBarrierItem {
  return {
    round_id: round.round_id,
    sub_account_id: round.sub_account_id,
    direction: round.direction,
    stop: round.stop,
    take_profit: Array.isArray(round.take_profit) ? round.take_profit : [],
  };
}

/**
 * Compute replay start timestamp N bars before the round's entry bar.
 */
export function replayStartBeforeEntry(
  openedTs: number,
  lookbackBars: number,
  tfMs: number,
): number {
  const safeTf = tfMs > 0 ? tfMs : 60_000;
  const barOpen = Math.floor(openedTs / safeTf) * safeTf;
  const bars = Math.max(1, Math.trunc(lookbackBars));
  return barOpen - bars * safeTf;
}

export interface BookedEquitySeries {
  sub_account_id: string;
  currency: string;
  points: Array<{
    seq: number;
    ts_event: number;
    equity: number;
    net_pnl: number;
    drawdown: number;
    max_drawdown: number;
  }>;
  latestEquity: number | null;
  latestNetPnl: number | null;
  maxDrawdown: number | null;
}

export function groupEquitySeries(
  points: readonly RunEquityPoint[],
): BookedEquitySeries[] {
  const grouped = new Map<string, BookedEquitySeries>();
  for (const pt of points) {
    const key = `${pt.sub_account_id}:${pt.currency}`;
    const entry = grouped.get(key) ?? {
      sub_account_id: pt.sub_account_id,
      currency: pt.currency,
      points: [],
      latestEquity: null,
      latestNetPnl: null,
      maxDrawdown: null,
    };
    const equity = Number(pt.equity);
    const net_pnl = Number(pt.net_pnl);
    const drawdown = Number(pt.drawdown);
    const max_drawdown = Number(pt.max_drawdown);
    if (
      Number.isFinite(equity) &&
      Number.isFinite(net_pnl) &&
      Number.isFinite(drawdown) &&
      Number.isFinite(max_drawdown)
    ) {
      entry.points.push({
        seq: pt.seq,
        ts_event: pt.ts_event,
        equity,
        net_pnl,
        drawdown,
        max_drawdown,
      });
      entry.latestEquity = equity;
      entry.latestNetPnl = net_pnl;
      entry.maxDrawdown = max_drawdown;
    }
    grouped.set(key, entry);
  }
  return [...grouped.values()];
}
