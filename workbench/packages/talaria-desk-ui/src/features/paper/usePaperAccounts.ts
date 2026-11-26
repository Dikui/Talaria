import { useCallback, useEffect, useMemo, useState } from "react";
import type { TradeExecution } from "@luxalgo/vela";
import type {
  PaperAccountView,
  PaperFill,
  PaperRound,
  RunFillReason,
  RunRoundFill,
  RunRoundRecord,
} from "@talaria/contracts";

import { api } from "../../lib/api";
import { deskSocketHub } from "../feed/deskSocketHub";
import { roundsToTradeExecutions } from "../runs/model";

const TERMINAL_PHASES = new Set(["CLOSED", "CANCELLED", "LIQUIDATED"]);

export function isPaperProcessOnline(view: PaperAccountView | null | undefined): boolean {
  return view?.process?.status === "RUNNING";
}

export function isActivePaperRound(round: PaperRound): boolean {
  if (round.phase) {
    return !TERMINAL_PHASES.has(round.phase);
  }
  const qty = Number(round.quantity);
  return Number.isFinite(qty) && qty > 0;
}

export function activePaperBarriersForInstrument(
  views: readonly PaperAccountView[],
  instrumentId: string,
): PaperRound[] {
  const result: PaperRound[] = [];
  for (const view of views) {
    const seen = new Set<string>();
    for (const round of view.rounds ?? []) {
      if (round.instrument_id === instrumentId && isActivePaperRound(round)) {
        seen.add(round.round_id);
        result.push(round);
      }
    }
    for (const pos of view.positions ?? []) {
      if (pos.instrument_id === instrumentId && isActivePaperRound(pos) && !seen.has(pos.round_id)) {
        seen.add(pos.round_id);
        result.push(pos);
      }
    }
  }
  return result;
}

function mapPaperFillReason(fill: PaperFill): RunFillReason {
  if (fill.kind === "ENTRY_FILLED") return "ENTRY";
  switch (fill.reason) {
    case "STOP_LOSS":
    case "TRAILING_STOP":
      return "SL";
    case "TAKE_PROFIT":
      return "TP";
    case "MAX_HOLDING_TIME":
      return "TIME";
    case "MARGIN_LIQUIDATION":
      return "LIQUIDATION";
    default:
      return "OTHER";
  }
}

/**
 * Convert `PaperAccountView` rounds & fills into `RunRoundRecord[]` so paper
 * fills reuse the exact same `roundsToTradeExecutions` marker mapping as backtest runs.
 */
export function paperViewToRunRounds(view: PaperAccountView): RunRoundRecord[] {
  const directionByRound = new Map<string, "LONG" | "SHORT">();
  for (const round of view.rounds ?? []) {
    directionByRound.set(round.round_id, round.direction);
  }
  for (const pos of view.positions ?? []) {
    if (!directionByRound.has(pos.round_id)) {
      directionByRound.set(pos.round_id, pos.direction);
    }
  }

  const fillsByRound = new Map<string, { instrumentId: string; fills: RunRoundFill[] }>();
  for (const fill of view.fills ?? []) {
    const direction = directionByRound.get(fill.round_id) ?? "LONG";
    const isBuy =
      direction === "LONG" ? fill.kind === "ENTRY_FILLED" : fill.kind !== "ENTRY_FILLED";
    const mapped: RunRoundFill = {
      event_id: fill.event_id,
      ts_event: fill.ts_event,
      kind: fill.kind,
      side: isBuy ? "buy" : "sell",
      price: fill.price,
      quantity: fill.quantity,
      reason: mapPaperFillReason(fill),
    };
    const bucket = fillsByRound.get(fill.round_id) ?? {
      instrumentId: fill.instrument_id,
      fills: [],
    };
    bucket.fills.push(mapped);
    fillsByRound.set(fill.round_id, bucket);
  }

  const records: RunRoundRecord[] = [];
  for (const [roundId, bucket] of fillsByRound.entries()) {
    records.push({
      round_id: roundId,
      sub_account_id: view.sub_account_id,
      currency: view.account_state?.currency ?? "USDT",
      instrument_id: bucket.instrumentId,
      direction: directionByRound.get(roundId) ?? "LONG",
      phase: "ACTIVE_PROTECTED",
      max_quantity: "0",
      remaining_quantity: "0",
      stop: "0",
      take_profit: [],
      net_pnl: "0",
      fees: "0",
      funding: "0",
      liquidation_fees: "0",
      fills: bucket.fills,
    });
  }
  return records;
}

export function paperViewsToTradeExecutions(
  views: readonly PaperAccountView[],
  tfMs: number,
  instrumentId?: string,
): TradeExecution[] {
  return roundsToTradeExecutions(
    views.flatMap((view) => paperViewToRunRounds(view)),
    tfMs,
    instrumentId,
  );
}

export interface PaperAccountsController {
  accounts: string[];
  viewsById: Record<string, PaperAccountView>;
  views: PaperAccountView[];
  selectedAccount: string;
  selectedView: PaperAccountView | null;
  selectAccount: (accountId: string) => void;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

export function usePaperAccounts(): PaperAccountsController {
  const [accounts, setAccounts] = useState<string[]>([]);
  const [viewsById, setViewsById] = useState<Record<string, PaperAccountView>>({});
  const [selectedAccount, setSelectedAccount] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const ids = await api.paperAccounts();
      const sorted = Array.isArray(ids) ? [...new Set(ids)].sort() : [];
      const fetched = await Promise.all(
        sorted.map(async (id) => {
          try {
            return await api.paperAccount(id);
          } catch {
            return null;
          }
        }),
      );
      const nextViews: Record<string, PaperAccountView> = {};
      for (const view of fetched) {
        if (view) {
          nextViews[view.sub_account_id] = view;
        }
      }
      setAccounts(sorted);
      setViewsById(nextViews);
      setError(null);
      setSelectedAccount((prev) =>
        prev && sorted.includes(prev) ? prev : (sorted[0] ?? ""),
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to load paper accounts");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    return deskSocketHub.subscribePaperAccounts((view) => {
      setViewsById((prev) => ({ ...prev, [view.sub_account_id]: view }));
      setAccounts((prev) => {
        if (prev.includes(view.sub_account_id)) return prev;
        return [...prev, view.sub_account_id].sort();
      });
      setSelectedAccount((prev) => prev || view.sub_account_id);
      setLoading(false);
      setError(null);
    });
  }, []);

  const effectiveSelected = accounts.includes(selectedAccount)
    ? selectedAccount
    : (accounts[0] ?? "");

  const views = useMemo(
    () => accounts.map((id) => viewsById[id]).filter((v): v is PaperAccountView => Boolean(v)),
    [accounts, viewsById],
  );

  const selectedView = effectiveSelected ? (viewsById[effectiveSelected] ?? null) : null;

  return {
    accounts,
    viewsById,
    views,
    selectedAccount: effectiveSelected,
    selectedView,
    selectAccount: setSelectedAccount,
    loading,
    error,
    refresh,
  };
}
