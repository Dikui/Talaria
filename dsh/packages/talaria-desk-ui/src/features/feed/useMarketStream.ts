import { useEffect, useReducer, useRef, useState } from "react";

import type {
  DerivativeTickEvent,
  FeedHealthEvent,
  KlineEvent,
  OrderBookEvent,
} from "@talaria/contracts";

import { parseDeskMessage } from "./messages";
import {
  feedReducer,
  initialFeedState,
  type FeedHello,
  type FeedState,
} from "./reducer";

export interface MarketStream extends FeedState {
  /** 每秒推进一次的本地时钟，用于 STALE 计时展示。 */
  now: number;
  feedHealth: FeedHealthEvent | null;
  latestBar: KlineEvent | null;
  hello: FeedHello | null;
  book: OrderBookEvent | null;
  derivatives: DerivativeTickEvent | null;
}

const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 8_000;

/** WS 地址由当前页面推导，兼容 http/https 与端口代理。 */
export function deskSocketUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws/desk`;
}

function subscribePayload(instrumentId: string, timeframe: string) {
  return JSON.stringify({
    op: "subscribe",
    instrument_id: instrumentId,
    timeframe,
  });
}

function unsubscribePayload(instrumentId: string, timeframe: string) {
  return JSON.stringify({
    op: "unsubscribe",
    instrument_id: instrumentId,
    timeframe,
  });
}

/**
 * 快通道订阅：单条 WS 连接承载 bar/book/health。
 *
 * - 断线后指数退避重连（0.5s → 8s），重连成功自动补订阅当前标的与周期；
 * - 切换标的或周期时先退订旧通道再订阅新通道；
 * - STALE 期间本地时钟每秒跳动，供“断流 N 秒”计时。
 */
export function useMarketStream(
  instrumentId: string,
  timeframe: string,
): MarketStream {
  const [state, dispatch] = useReducer(feedReducer, initialFeedState);
  const [now, setNow] = useState(() => Date.now());
  const socketRef = useRef<WebSocket | null>(null);
  const attemptsRef = useRef(0);
  const retryRef = useRef<number | null>(null);
  const currentRef = useRef({ instrumentId, timeframe });
  const sentRef = useRef<{
    socket: WebSocket;
    instrumentId: string;
    timeframe: string;
  } | null>(null);
  currentRef.current = { instrumentId, timeframe };

  useEffect(() => {
    let disposed = false;

    const connect = () => {
      if (disposed) {
        return;
      }
      dispatch({ type: "connecting" });
      let socket: WebSocket;
      try {
        socket = new WebSocket(deskSocketUrl());
      } catch {
        dispatch({ type: "closed" });
        return;
      }
      socketRef.current = socket;

      socket.onopen = () => {
        attemptsRef.current = 0;
        dispatch({ type: "opened" });
        const current = currentRef.current;
        socket.send(subscribePayload(current.instrumentId, current.timeframe));
        sentRef.current = { socket, ...current };
      };

      socket.onmessage = (event) => {
        const message = parseDeskMessage(String(event.data));
        if (message !== null) {
          dispatch({ type: "message", message });
        }
      };

      socket.onerror = () => {
        socket.close();
      };

      socket.onclose = () => {
        dispatch({ type: "closed" });
        if (socketRef.current === socket) {
          socketRef.current = null;
        }
        sentRef.current = null;
        if (disposed) {
          return;
        }
        const delay = Math.min(
          RECONNECT_BASE_MS * 2 ** attemptsRef.current,
          RECONNECT_MAX_MS,
        );
        attemptsRef.current += 1;
        retryRef.current = window.setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      disposed = true;
      if (retryRef.current !== null) {
        window.clearTimeout(retryRef.current);
        retryRef.current = null;
      }
      const socket = socketRef.current;
      socketRef.current = null;
      sentRef.current = null;
      socket?.close();
    };
  }, []);

  useEffect(() => {
    dispatch({ type: "reset" });
    const socket = socketRef.current;
    if (socket === null || socket.readyState !== WebSocket.OPEN) {
      return;
    }
    const sent = sentRef.current;
    if (
      sent !== null &&
      sent.socket === socket &&
      sent.instrumentId === instrumentId &&
      sent.timeframe === timeframe
    ) {
      return;
    }
    if (sent !== null && sent.socket === socket) {
      socket.send(unsubscribePayload(sent.instrumentId, sent.timeframe));
    }
    socket.send(subscribePayload(instrumentId, timeframe));
    sentRef.current = { socket, instrumentId, timeframe };
  }, [instrumentId, timeframe]);

  const status = state.health?.status ?? null;
  const staleSince = state.health?.stale_since ?? null;

  useEffect(() => {
    if (status !== "STALE") {
      return;
    }
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
