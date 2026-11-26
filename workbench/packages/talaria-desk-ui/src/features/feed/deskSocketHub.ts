import type {
  AlertTriggerRecord,
  DerivativeTickEvent,
  FeedHealthEvent,
  KeyLevelSet,
  KlineEvent,
  OrderBookEvent,
  PaperAccountView,
  ScannerTrigger,
} from "@talaria/contracts";

import { getApiBaseUrl, getApiToken } from "../../lib/api";
import { parseDeskMessage, type DeskMessage } from "./messages";
import type { ConnectionState, FeedHello } from "./reducer";

const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 8_000;

export function deskSocketUrl(): string {
  const base = getApiBaseUrl();
  const token = getApiToken();
  let url: string;
  if (base) {
    url = `${base.replace(/^http/, "ws")}/ws/desk`;
  } else {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    url = `${protocol}//${window.location.host}/ws/desk`;
  }
  if (token && base) {
    return `${url}?token=${encodeURIComponent(token)}`;
  }
  return url;
}

export interface DeskChannelCallbacks {
  onConnection?: (state: ConnectionState) => void;
  onHello?: (hello: FeedHello, deniedAlerts: string[]) => void;
  onChannelMode?: (realtime: boolean) => void;
  onBar?: (event: KlineEvent) => void;
  onBook?: (event: OrderBookEvent) => void;
  onDerivatives?: (event: DerivativeTickEvent) => void;
  onHealth?: (event: FeedHealthEvent) => void;
  onLevels?: (levels: KeyLevelSet) => void;
  onAlert?: (alert: AlertTriggerRecord) => void;
  onAlertQuota?: (deniedAlerts: string[]) => void;
  onScanner?: (trigger: ScannerTrigger) => void;
  onPaperAccount?: (view: PaperAccountView) => void;
  onError?: (msg: Extract<DeskMessage, { type: "error" }>) => void;
}

interface SubscriptionEntry {
  instrumentId: string;
  timeframe: string;
  callbacks: DeskChannelCallbacks;
}

/**
 * Shared multiplexed `/ws/desk` client.
 * Reference-counts `(instrument_id, timeframe)` subscriptions across all panes in
 * a multi-chart layout so we stay well within `talaria-server` connection limits.
 */
export class DeskSocketHub {
  private socket: WebSocket | null = null;
  private connection: ConnectionState = "closed";
  private hello: FeedHello | null = null;
  private deniedAlerts: string[] = [];
  private attempts = 0;
  private retryTimer: number | null = null;
  private quotaTimers = new Map<string, number>();
  private subscribers = new Map<number, SubscriptionEntry>();
  private paperAccountListeners = new Map<number, (view: PaperAccountView) => void>();
  private activeChannels = new Map<string, number>();
  private channelModes = new Map<string, boolean>();
  private nextId = 1;
  private disposed = false;

  private channelKey(instrumentId: string, timeframe: string): string {
    return `${instrumentId}\u0000${timeframe}`;
  }

  private ensureConnected(): void {
    if (this.disposed || this.socket !== null) return;
    if (typeof WebSocket === "undefined") return;

    this.setConnection("connecting");
    let socket: WebSocket;
    try {
      socket = new WebSocket(deskSocketUrl());
    } catch {
      this.setConnection("closed");
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      if (this.socket !== socket || this.disposed) return;
      this.attempts = 0;
      this.setConnection("open");
      for (const [key, count] of this.activeChannels.entries()) {
        if (count <= 0) continue;
        const [instrumentId, timeframe] = key.split("\u0000");
        this.sendOp(socket, "subscribe", instrumentId, timeframe);
      }
    };

    socket.onmessage = (event) => {
      if (this.socket !== socket || this.disposed) return;
      const msg = parseDeskMessage(String(event.data));
      if (!msg) return;
      this.dispatchMessage(socket, msg);
    };

    socket.onerror = () => {
      socket.close();
    };

    socket.onclose = () => {
      if (this.socket === socket) {
        this.socket = null;
      }
      this.channelModes.clear();
      this.setConnection("closed");
      if (!this.disposed && (this.subscribers.size > 0 || this.paperAccountListeners.size > 0)) {
        this.scheduleReconnect();
      }
    };
  }

  private scheduleReconnect(): void {
    if (this.retryTimer !== null || this.disposed) return;
    const delay = Math.min(RECONNECT_BASE_MS * 2 ** this.attempts, RECONNECT_MAX_MS);
    this.attempts += 1;
    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = null;
      this.ensureConnected();
    }, delay);
  }

  private setConnection(next: ConnectionState): void {
    this.connection = next;
    for (const sub of this.subscribers.values()) {
      sub.callbacks.onConnection?.(next);
    }
  }

  private sendOp(
    socket: WebSocket,
    op: "subscribe" | "unsubscribe",
    instrumentId: string,
    timeframe: string,
  ): void {
    if (socket.readyState !== WebSocket.OPEN) return;
    socket.send(
      JSON.stringify({
        op,
        instrument_id: instrumentId,
        timeframe,
      }),
    );
  }

  private dispatchMessage(socket: WebSocket, msg: DeskMessage): void {
    switch (msg.type) {
      case "hello":
        this.hello = {
          feed_mode: msg.feed_mode,
          server_ts: msg.server_ts,
          live: msg.live,
        };
        this.deniedAlerts = msg.denied_alerts ?? [];
        for (const sub of this.subscribers.values()) {
          sub.callbacks.onHello?.(this.hello, this.deniedAlerts);
        }
        break;
      case "channel_mode": {
        const key = this.channelKey(msg.instrument_id, msg.timeframe);
        this.channelModes.set(key, msg.realtime);
        for (const sub of this.subscribers.values()) {
          if (
            sub.instrumentId === msg.instrument_id &&
            sub.timeframe === msg.timeframe
          ) {
            sub.callbacks.onChannelMode?.(msg.realtime);
          }
        }
        break;
      }
      case "alert_quota":
        this.deniedAlerts = msg.denied_alerts;
        for (const sub of this.subscribers.values()) {
          sub.callbacks.onAlertQuota?.(msg.denied_alerts);
        }
        break;
      case "bar":
        for (const sub of this.subscribers.values()) {
          if (
            sub.instrumentId === msg.data.instrument_id &&
            sub.timeframe === msg.data.timeframe
          ) {
            sub.callbacks.onBar?.(msg.data);
          }
        }
        break;
      case "health": {
        const key = this.channelKey(msg.data.instrument_id, msg.data.timeframe);
        const qt = this.quotaTimers.get(key);
        if (qt !== undefined) {
          window.clearTimeout(qt);
          this.quotaTimers.delete(key);
        }
        for (const sub of this.subscribers.values()) {
          if (
            sub.instrumentId === msg.data.instrument_id &&
            sub.timeframe === msg.data.timeframe
          ) {
            sub.callbacks.onHealth?.(msg.data);
          }
        }
        break;
      }
      case "book":
        for (const sub of this.subscribers.values()) {
          if (sub.instrumentId === msg.data.instrument_id) {
            sub.callbacks.onBook?.(msg.data);
          }
        }
        break;
      case "derivatives":
        for (const sub of this.subscribers.values()) {
          if (sub.instrumentId === msg.data.instrument_id) {
            sub.callbacks.onDerivatives?.(msg.data);
          }
        }
        break;
      case "levels":
        for (const sub of this.subscribers.values()) {
          if (
            sub.instrumentId === msg.data.instrument_id &&
            sub.timeframe === msg.data.timeframe
          ) {
            sub.callbacks.onLevels?.(msg.data);
          }
        }
        break;
      case "alert":
        for (const sub of this.subscribers.values()) {
          sub.callbacks.onAlert?.(msg.data);
        }
        break;
      case "scanner":
        for (const sub of this.subscribers.values()) {
          sub.callbacks.onScanner?.(msg.data);
        }
        break;
      case "paper_account":
        for (const sub of this.subscribers.values()) {
          sub.callbacks.onPaperAccount?.(msg.data);
        }
        for (const listener of this.paperAccountListeners.values()) {
          listener(msg.data);
        }
        break;
      case "error": {
        if (
          msg.code === "SUBSCRIPTION_QUOTA" &&
          msg.owner === "chart" &&
          msg.instrument_id &&
          msg.timeframe
        ) {
          const key = this.channelKey(msg.instrument_id, msg.timeframe);
          const existingTimer = this.quotaTimers.get(key);
          if (existingTimer !== undefined) {
            window.clearTimeout(existingTimer);
          }
          const inst = msg.instrument_id;
          const tf = msg.timeframe;
          const timer = window.setTimeout(() => {
            this.quotaTimers.delete(key);
            if (
              this.socket === socket &&
              socket.readyState === WebSocket.OPEN &&
              (this.activeChannels.get(key) ?? 0) > 0
            ) {
              this.sendOp(socket, "subscribe", inst, tf);
            }
          }, 1000);
          this.quotaTimers.set(key, timer);
        }
        for (const sub of this.subscribers.values()) {
          if (
            !msg.instrument_id ||
            (sub.instrumentId === msg.instrument_id &&
              (!msg.timeframe || sub.timeframe === msg.timeframe))
          ) {
            sub.callbacks.onError?.(msg);
          }
        }
        break;
      }
    }
  }

  subscribe(
    instrumentId: string,
    timeframe: string,
    callbacks: DeskChannelCallbacks,
  ): () => void {
    this.disposed = false;
    const id = this.nextId++;
    this.subscribers.set(id, { instrumentId, timeframe, callbacks });

    const key = this.channelKey(instrumentId, timeframe);
    const prevCount = this.activeChannels.get(key) ?? 0;
    this.activeChannels.set(key, prevCount + 1);

    callbacks.onConnection?.(this.connection);
    if (this.hello) {
      callbacks.onHello?.(this.hello, this.deniedAlerts);
    }
    const cachedMode = this.channelModes.get(key);
    if (cachedMode !== undefined) {
      callbacks.onChannelMode?.(cachedMode);
    }

    if (prevCount === 0 && this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.sendOp(this.socket, "subscribe", instrumentId, timeframe);
    }
    this.ensureConnected();

    return () => {
      this.subscribers.delete(id);
      const current = this.activeChannels.get(key) ?? 0;
      if (current <= 1) {
        this.activeChannels.delete(key);
        this.channelModes.delete(key);
        const qt = this.quotaTimers.get(key);
        if (qt !== undefined) {
          window.clearTimeout(qt);
          this.quotaTimers.delete(key);
        }
        if (this.socket && this.socket.readyState === WebSocket.OPEN) {
          this.sendOp(this.socket, "unsubscribe", instrumentId, timeframe);
        }
      } else {
        this.activeChannels.set(key, current - 1);
      }

      if (this.subscribers.size === 0 && this.paperAccountListeners.size === 0) {
        this.closeIfIdle();
      }
    };
  }

  subscribePaperAccounts(listener: (view: PaperAccountView) => void): () => void {
    this.disposed = false;
    const id = this.nextId++;
    this.paperAccountListeners.set(id, listener);
    this.ensureConnected();
    return () => {
      this.paperAccountListeners.delete(id);
      if (this.subscribers.size === 0 && this.paperAccountListeners.size === 0) {
        this.closeIfIdle();
      }
    };
  }

  private closeIfIdle(): void {
    if (this.retryTimer !== null) {
      window.clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    const socket = this.socket;
    this.socket = null;
    socket?.close();
    this.connection = "closed";
    this.hello = null;
    this.channelModes.clear();
  }

  destroy(): void {
    this.disposed = true;
    for (const timer of this.quotaTimers.values()) {
      window.clearTimeout(timer);
    }
    this.quotaTimers.clear();
    this.subscribers.clear();
    this.paperAccountListeners.clear();
    this.activeChannels.clear();
    this.closeIfIdle();
  }
}

export const deskSocketHub = new DeskSocketHub();
