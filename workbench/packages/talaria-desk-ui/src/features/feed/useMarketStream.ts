import { useEffect, useReducer, useState } from "react";

import type {
  AlertTriggerRecord,
  DerivativeTickEvent,
  FeedHealthEvent,
  KlineEvent,
  OrderBookEvent,
} from "@talaria/contracts";

import { deskSocketHub } from "./deskSocketHub";
import {
  feedReducer,
  initialFeedState,
  type FeedHello,
  type FeedState,
} from "./reducer";

export interface MarketStream extends FeedState {
  now: number;
  feedHealth: FeedHealthEvent | null;
  latestBar: KlineEvent | null;
  hello: FeedHello | null;
  book: OrderBookEvent | null;
  derivatives: DerivativeTickEvent | null;
  alert: AlertTriggerRecord | null;
}

export function useMarketStream(
  instrumentId: string,
  timeframe: string,
): MarketStream {
  const [state, dispatch] = useReducer(feedReducer, initialFeedState);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    dispatch({ type: "reset" });
    return deskSocketHub.subscribe(instrumentId, timeframe, {
      onConnection(conn) {
        if (conn === "connecting") dispatch({ type: "connecting" });
        else if (conn === "open") dispatch({ type: "opened" });
        else dispatch({ type: "closed" });
      },
      onHello(hello, deniedAlerts) {
        dispatch({
          type: "message",
          message: {
            type: "hello",
            feed_mode: hello.feed_mode,
            server_ts: hello.server_ts,
            live: hello.live,
            denied_alerts: deniedAlerts,
          },
        });
      },
      onChannelMode(realtime) {
        dispatch({
          type: "message",
          message: {
            type: "channel_mode",
            instrument_id: instrumentId,
            timeframe,
            realtime,
          },
        });
      },
      onAlertQuota(deniedAlerts) {
        dispatch({
          type: "message",
          message: { type: "alert_quota", denied_alerts: deniedAlerts },
        });
      },
      onBar(data) {
        dispatch({ type: "message", message: { type: "bar", data } });
      },
      onBook(data) {
        dispatch({ type: "message", message: { type: "book", data } });
      },
      onDerivatives(data) {
        dispatch({ type: "message", message: { type: "derivatives", data } });
      },
      onHealth(data) {
        dispatch({ type: "message", message: { type: "health", data } });
      },
      onLevels(data) {
        dispatch({ type: "message", message: { type: "levels", data } });
      },
      onStructures(data) {
        dispatch({ type: "message", message: { type: "structures", data } });
      },
      onAlert(data) {
        dispatch({ type: "message", message: { type: "alert", data } });
      },
      onScanner(data) {
        dispatch({ type: "message", message: { type: "scanner", data } });
      },
      onError(err) {
        dispatch({ type: "message", message: err });
      },
    });
  }, [instrumentId, timeframe]);

  const status = state.health?.status ?? null;
  const staleSince = state.health?.stale_since ?? null;

  useEffect(() => {
    if (status !== "STALE") return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [status, staleSince]);

  return {
    ...state,
    now,
    feedHealth: state.health,
    latestBar: state.bars.length > 0 ? state.bars[state.bars.length - 1] : null,
  };
}
