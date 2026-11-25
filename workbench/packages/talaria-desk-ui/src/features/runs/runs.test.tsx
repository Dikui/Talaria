import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type {
  RunEquityPoint,
  RunExportSummary,
  RunRoundRecord,
} from "@talaria/contracts";

import { selectVisibleTradeMarkers } from "../../chart/velaIndicators";
import { RunsPanel } from "./RunsPanel";
import {
  buildRunPluginPairs,
  replayStartBeforeEntry,
  roundsToTradeExecutions,
  runRoundToBarrier,
} from "./model";
import type { RunsController } from "./useRuns";

describe("Track B: Runs panel, tradeMarkers, ExecutionBarrierItem mapping & replay jump", () => {
  const sampleRuns: RunExportSummary[] = [
    {
      run_id: "run-l1",
      status: "succeeded",
      scheme: "breakout-v1",
      fill_plugin: "l1_trade_sim",
      instrument_id: "BTC-USDT-PERP",
      timeframe: "5m",
      start: 1_700_000_000_000,
      end: 1_700_003_600_000,
      pnl_basis: "booked",
      accounts: [
        {
          sub_account_id: "main",
          currency: "USDT",
          round_count: 2,
          trade_count: 4,
          net_pnl: "120.50",
          max_drawdown: "25.00",
          fees: "4.50",
          funding: "0",
          liquidation_fees: "0",
        },
      ],
    },
    {
      run_id: "run-bar",
      status: "succeeded",
      scheme: "breakout-v1",
      fill_plugin: "bar_sim_1m",
      instrument_id: "BTC-USDT-PERP",
      timeframe: "5m",
      start: 1_700_000_000_000,
      end: 1_700_003_600_000,
      pnl_basis: "booked",
      accounts: [
        {
          sub_account_id: "main",
          currency: "USDT",
          round_count: 2,
          trade_count: 4,
          net_pnl: "135.00",
          max_drawdown: "18.00",
          fees: "4.50",
          funding: "0",
          liquidation_fees: "0",
        },
      ],
    },
  ];

  const sampleRounds: RunRoundRecord[] = [
    {
      round_id: "round-1",
      sub_account_id: "main",
      currency: "USDT",
      instrument_id: "BTC-USDT-PERP",
      direction: "LONG",
      phase: "CLOSED",
      opened_ts: 1_700_000_120_000, // inside 5m slot 1_700_000_000_000..300_000
      closed_ts: 1_700_000_450_000,
      entry_price: "50000.5",
      exit_price: "50250.0",
      max_quantity: "0.5",
      remaining_quantity: "0",
      stop: "49800.0",
      take_profit: ["50250.0", "50500.0"],
      exit_reason: "TP",
      net_pnl: "124.75",
      fees: "2.25",
      funding: "0",
      liquidation_fees: "0",
      mae: "-35.0",
      mfe: "140.0",
      fills: [
        {
          event_id: "f1",
          ts_event: 1_700_000_125_000,
          kind: "ENTRY_FILLED",
          side: "buy",
          price: "50000.5",
          quantity: "0.5",
          reason: "ENTRY",
        },
        {
          event_id: "f2",
          ts_event: 1_700_000_450_000,
          kind: "EXIT_FILLED",
          side: "sell",
          price: "50250.0",
          quantity: "0.5",
          reason: "TP",
        },
      ],
    },
  ];

  const sampleEquity: RunEquityPoint[] = [
    {
      sub_account_id: "main",
      currency: "USDT",
      seq: 1,
      ts_event: 1_700_000_450_000,
      event_id: "e1",
      event_kind: "EXIT_FILLED",
      round_id: "round-1",
      pnl_basis: "booked",
      realized_pnl_delta: "124.75",
      fee_delta: "2.25",
      funding_delta: "0",
      liquidation_fee_delta: "0",
      net_pnl: "122.50",
      equity: "10122.50",
      peak_pnl: "122.50",
      drawdown: "0",
      max_drawdown: "25.00",
    },
  ];

  it("maps fills to Vela TradeExecution aligned by Math.floor(ts_event / tf_ms) * tf_ms and filters during replay", () => {
    const tf5m = 300_000;
    const markers = roundsToTradeExecutions(sampleRounds, tf5m, "BTC-USDT-PERP");

    expect(markers).toEqual([
      {
        time: Math.floor(1_700_000_125_000 / tf5m) * tf5m,
        price: 50000.5,
        side: "buy",
        kind: "entry",
        qty: 0.5,
        tradeId: "round-1",
        label: "ENTRY",
      },
      {
        time: Math.floor(1_700_000_450_000 / tf5m) * tf5m,
        price: 50250.0,
        side: "sell",
        kind: "exit",
        qty: 0.5,
        tradeId: "round-1",
        label: "TP",
      },
    ]);

    // During replay before exit bar is revealed, only entry marker is emitted
    const visibleAtStart = selectVisibleTradeMarkers(markers, [{ time: markers[0].time }]);
    expect(visibleAtStart).toHaveLength(1);
    expect(visibleAtStart[0].label).toBe("ENTRY");
  });

  it("maps selected round stop/TP to ExecutionBarrierItem and computes l1_trade_sim vs bar_sim_1m delta", () => {
    const barrier = runRoundToBarrier(sampleRounds[0]);
    expect(barrier).toEqual({
      round_id: "round-1",
      sub_account_id: "main",
      direction: "LONG",
      stop: "49800.0",
      take_profit: ["50250.0", "50500.0"],
    });

    const pairs = buildRunPluginPairs(sampleRuns);
    expect(pairs).toHaveLength(1);
    expect(pairs[0].deltas[0].netPnlDelta).toBeCloseTo(-14.5, 6);
    expect(pairs[0].deltas[0].maxDrawdownDelta).toBeCloseTo(7.0, 6);
  });

  it("renders RunsPanel with 已实现口径, MAE/MFE, jump-to-round, and N-bar pre-entry replay", () => {
    const onJumpToRound = vi.fn();
    const onReplayBeforeRound = vi.fn();
    const selectRound = vi.fn();

    const controller: RunsController = {
      runs: sampleRuns,
      selectedRunId: "run-l1",
      selectedRun: sampleRuns[0],
      rounds: sampleRounds,
      equityPoints: sampleEquity,
      selectedRoundId: "round-1",
      loadingRuns: false,
      loadingDetail: false,
      error: null,
      refresh: vi.fn(),
      selectRun: vi.fn(),
      selectRound,
    };

    render(
      <RunsPanel
        controller={controller}
        activeTimeframe="5m"
        onJumpToRound={onJumpToRound}
        onReplayBeforeRound={onReplayBeforeRound}
      />,
    );

    expect(screen.getByTestId("runs-basis-badge").textContent).toContain("已实现口径");
    expect(screen.getByTestId("runs-plugin-deltas").textContent).toContain("-14.50");
    expect(screen.getByTestId("run-round-mae-mfe-round-1").textContent).toContain("-35.0");
    expect(screen.getByTestId("run-round-mae-mfe-round-1").textContent).toContain("140.0");

    // Click round -> select + jump to entry time
    const roundItem = screen.getByTestId("run-round-round-1");
    fireEvent.click(roundItem.querySelector("button")!);
    expect(selectRound).toHaveBeenCalledWith("round-1");
    expect(onJumpToRound).toHaveBeenCalledWith(sampleRounds[0], "5m");

    // Click one-click replay N=20 bars before entry
    fireEvent.click(screen.getByTestId("run-round-replay-round-1"));
    expect(onReplayBeforeRound).toHaveBeenCalledWith({
      instrumentId: "BTC-USDT-PERP",
      timeframe: "5m",
      fromTs: replayStartBeforeEntry(sampleRounds[0].opened_ts!, 20, 300_000),
    });
  });
});
