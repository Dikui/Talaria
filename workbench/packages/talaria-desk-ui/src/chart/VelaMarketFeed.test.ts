import { describe, expect, it, vi } from "vitest";
import type { CompactBar, FeedHealthEvent, KlineEvent, SnapshotMeta } from "@talaria/contracts";

import { VelaMarketFeed } from "./VelaMarketFeed";
import { compactBarsToVelaSeries } from "../lib/chartData";
import { feedAlertView, feedBadgeView, staleDurationMs } from "../features/feed/health";
import { parseDeskMessage } from "../features/feed/messages";
import { feedReducer, initialFeedState } from "../features/feed/reducer";

function makeBar(timestamp: number, status: CompactBar["status"] = "NORMAL"): CompactBar {
  return {
    timestamp,
    open: status === "FEED_GAP" ? null : "68000.0",
    high: status === "FEED_GAP" ? null : "68010.0",
    low: status === "FEED_GAP" ? null : "67990.0",
    close: status === "FEED_GAP" ? null : "68005.0",
    volume: status === "FEED_GAP" ? null : "1.5",
    turnover: status === "FEED_GAP" ? null : "102000.0",
    status,
    ts_available: timestamp + 60_000,
    precision: "SECOND_VERIFIED",
  };
}

describe("VelaMarketFeed and FEED_GAP / STALE / RECOVERING semantics", () => {
  it("never fabricates synthetic candles for FEED_GAP slots", () => {
    const bars: CompactBar[] = [
      makeBar(1_700_000_060_000, "NORMAL"),
      makeBar(1_700_000_120_000, "FEED_GAP"),
      makeBar(1_700_000_180_000, "FEED_GAP"),
      makeBar(1_700_000_240_000, "NO_TRADES"),
    ];

    const series = compactBarsToVelaSeries(bars);
    expect(series.bars).toHaveLength(2);
    expect(series.bars.map((b) => b.time)).toEqual([
      1_700_000_060_000,
      1_700_000_240_000,
    ]);
    expect(series.gapCount).toBe(2);
    expect(series.gapTimestamps).toEqual([1_700_000_120_000, 1_700_000_180_000]);
  });

  it("loads snapshot bars, skips live FEED_GAP ticks, and expands historical depth on loadRange", async () => {
    const statsSpy = vi.fn();
    let liveBarCb: ((event: KlineEvent) => void) | null = null;

    const snapshotMeta: SnapshotMeta = {
      snapshot_id: "snap-001",
      instrument_id: "BTC-USDT-PERP",
      timeframes: ["1m"],
      cutoff_ts: 1_700_000_300_000,
      created_ts: 1_700_000_300_000,
      mode: "HISTORICAL_PIT",
      bar_counts: { "1m": 3 },
    };

    const feed = new VelaMarketFeed({
      onStats: statsSpy,
      client: {
        createSnapshot: vi.fn().mockResolvedValue(snapshotMeta),
        bars: vi
          .fn()
          .mockResolvedValueOnce({
            snapshot_id: "snap-001",
            instrument_id: "BTC-USDT-PERP",
            timeframe: "1m",
            bars: [
              makeBar(1_700_000_180_000, "NORMAL"),
              makeBar(1_700_000_240_000, "FEED_GAP"),
              makeBar(1_700_000_300_000, "NORMAL"),
            ],
            has_more_backward: true,
            has_more_forward: false,
          })
          .mockResolvedValueOnce({
            snapshot_id: "snap-001",
            instrument_id: "BTC-USDT-PERP",
            timeframe: "1m",
            bars: [
              makeBar(1_700_000_060_000, "NORMAL"),
              makeBar(1_700_000_120_000, "NORMAL"),
            ],
            has_more_backward: false,
            has_more_forward: true,
          }),
      },
      subscribeLiveStream: (_inst, _tf, handlers) => {
        liveBarCb = handlers.onBar;
        return () => {
          liveBarCb = null;
        };
      },
    });

    const initial = await feed.load({ symbol: "BTC-USDT-PERP", timeframe: "1m", bars: 300 });
    expect(initial.map((b) => b.time)).toEqual([1_700_000_180_000, 1_700_000_300_000]);
    expect(statsSpy).toHaveBeenLastCalledWith({ loaded: 2, gaps: 1 });
    expect(feed.canExpandBackward()).toBe(true);

    const older = await feed.loadRange(
      { symbol: "BTC-USDT-PERP", timeframe: "1m" },
      { to: 1_700_000_180_000, limit: 300 },
    );
    expect(older.map((b) => b.time)).toEqual([1_700_000_060_000, 1_700_000_120_000]);
    expect(feed.canExpandBackward()).toBe(false);
    expect(statsSpy).toHaveBeenLastCalledWith({ loaded: 4, gaps: 1 });

    const pushed: number[] = [];
    const unsub = feed.subscribe(
      { symbol: "BTC-USDT-PERP", timeframe: "1m" },
      (bar) => pushed.push(bar.time),
    );
    expect(liveBarCb).not.toBeNull();
    liveBarCb!({
      instrument_id: "BTC-USDT-PERP",
      timeframe: "1m",
      bar: makeBar(1_700_000_360_000, "FEED_GAP"),
      confirmed: true,
      revision: 0,
    });
    expect(pushed).toEqual([]);
    expect(statsSpy).toHaveBeenLastCalledWith({ loaded: 4, gaps: 2 });

    liveBarCb!({
      instrument_id: "BTC-USDT-PERP",
      timeframe: "1m",
      bar: makeBar(1_700_000_420_000, "NORMAL"),
      confirmed: true,
      revision: 0,
    });
    expect(pushed).toEqual([1_700_000_420_000]);
    expect(statsSpy).toHaveBeenLastCalledWith({ loaded: 5, gaps: 2 });
    unsub();
  });

  it("tracks LIVE -> STALE (timer) -> RECOVERING -> LIVE transitions accurately", () => {
    const staleAt = 1_700_000_000_000;
    const staleEvent: FeedHealthEvent = {
      instrument_id: "BTC-USDT-PERP",
      timeframe: "1m",
      status: "STALE",
      ts: staleAt + 14_000,
      last_event_ts: staleAt,
      stale_since: staleAt,
      reconnect_count: 1,
    };

    let state = feedReducer(initialFeedState, { type: "opened" });
    state = feedReducer(state, {
      type: "message",
      message: { type: "hello", feed_mode: "replay", server_ts: staleAt, live: true },
    });
    state = feedReducer(state, {
      type: "message",
      message: { type: "health", data: staleEvent },
    });

    const now = staleAt + 18_500;
    expect(staleDurationMs(state.health, now)).toBe(18_500);
    const staleBadge = feedBadgeView(state.health, now, true, state.connection, "en");
    expect(staleBadge.tone).toBe("stale");
    expect(staleBadge.label).toBe("Stream stale 18s");
    expect(feedAlertView(state.health, now, true, state.connection)).not.toBeNull();

    state = feedReducer(state, {
      type: "message",
      message: {
        type: "health",
        data: { ...staleEvent, status: "RECOVERING", ts: now + 500, stale_since: null },
      },
    });
    const recoveringBadge = feedBadgeView(state.health, now + 500, true, state.connection, "en");
    expect(recoveringBadge.tone).toBe("recovering");
    expect(recoveringBadge.label).toBe("Recovering");

    state = feedReducer(state, {
      type: "message",
      message: {
        type: "health",
        data: { ...staleEvent, status: "LIVE", ts: now + 1500, stale_since: null },
      },
    });
    const liveBadge = feedBadgeView(state.health, now + 1500, true, state.connection, "en");
    expect(liveBadge.tone).toBe("live");
    expect(liveBadge.label).toBe("Live");
    expect(feedAlertView(state.health, now + 1500, true, state.connection)).toBeNull();
  });

  it("shows ARCHIVE badge and suppresses alerts when channel_mode has realtime=false", () => {
    const parsed = parseDeskMessage(
      JSON.stringify({
        type: "channel_mode",
        instrument_id: "BTC-BN-USDT-PERP",
        timeframe: "1m",
        realtime: false,
      }),
    );
    expect(parsed).toEqual({
      type: "channel_mode",
      instrument_id: "BTC-BN-USDT-PERP",
      timeframe: "1m",
      realtime: false,
    });

    let state = feedReducer(initialFeedState, { type: "opened" });
    state = feedReducer(state, {
      type: "message",
      message: { type: "hello", feed_mode: "live", server_ts: 1_700_000_000_000, live: true },
    });
    state = feedReducer(state, { type: "message", message: parsed! });
    expect(state.channelRealtime).toBe(false);

    const now = 1_700_000_030_000;
    const badgeEn = feedBadgeView(
      null,
      now,
      true,
      state.connection,
      "en",
      state.channelRealtime,
    );
    expect(badgeEn).toEqual({
      status: "ARCHIVE",
      label: "Archive",
      tone: "offline",
    });

    const badgeZh = feedBadgeView(
      null,
      now,
      true,
      state.connection,
      "zh",
      state.channelRealtime,
    );
    expect(badgeZh).toEqual({
      status: "ARCHIVE",
      label: "历史归档",
      tone: "offline",
    });

    const staleHealth: FeedHealthEvent = {
      instrument_id: "BTC-BN-USDT-PERP",
      timeframe: "1m",
      status: "STALE",
      ts: now,
      last_event_ts: now - 20_000,
      stale_since: now - 20_000,
      reconnect_count: 2,
    };
    const archiveOverStale = feedBadgeView(
      staleHealth,
      now,
      true,
      state.connection,
      "en",
      state.channelRealtime,
    );
    expect(archiveOverStale.status).toBe("ARCHIVE");
    expect(archiveOverStale.tone).toBe("offline");
    expect(
      feedAlertView(staleHealth, now, true, state.connection, "en", state.channelRealtime),
    ).toBeNull();

    const resetState = feedReducer(state, { type: "reset" });
    expect(resetState.channelRealtime).toBeNull();
  });
});
