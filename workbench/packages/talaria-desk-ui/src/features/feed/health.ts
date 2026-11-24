import type { FeedHealthEvent } from "@talaria/contracts";

import { getLocale, tFor, type Locale } from "../../lib/i18n";
import type { ConnectionState } from "./reducer";

export type FeedTone = "live" | "recovering" | "stale";
export type FeedBadgeTone = FeedTone | "offline" | "waiting" | "disconnected";

export const STALE_AFTER_MS = 12_000;

export function staleDurationMs(health: FeedHealthEvent | null, now: number): number {
  if (health === null || health.status !== "STALE" || health.stale_since == null) {
    return 0;
  }
  return Math.max(now - health.stale_since, 0);
}

export function formatDuration(ms: number, locale: Locale = getLocale()): string {
  const seconds = Math.floor(Math.max(ms, 0) / 1000);
  if (locale === "en") {
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    const rest = seconds % 60;
    return rest === 0 ? `${minutes}m` : `${minutes}m ${rest}s`;
  }
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest === 0 ? `${minutes} 分` : `${minutes} 分 ${rest} 秒`;
}

export function healthTone(status: FeedHealthEvent["status"]): FeedTone {
  if (status === "LIVE") return "live";
  if (status === "RECOVERING") return "recovering";
  return "stale";
}

export function healthLabel(
  health: FeedHealthEvent | null,
  now: number,
  locale: Locale = getLocale(),
): string {
  if (health === null) {
    return tFor(locale, "等待行情", "Waiting for market data");
  }
  if (health.status === "LIVE") {
    return tFor(locale, "实时", "Live");
  }
  if (health.status === "RECOVERING") {
    return tFor(locale, "恢复中", "Recovering");
  }
  const duration = formatDuration(staleDurationMs(health, now), locale);
  return tFor(locale, `断流 ${duration}`, `Stream stale ${duration}`);
}

export function isStaleWindowExceeded(health: FeedHealthEvent | null, now: number): boolean {
  return (
    health !== null &&
    health.status === "STALE" &&
    staleDurationMs(health, now) >= STALE_AFTER_MS
  );
}

function browserDisconnected(live: boolean | null, connection: ConnectionState): boolean {
  if (live === false) return false;
  if (connection === "closed") return true;
  return connection === "connecting" && live === true;
}

export interface FeedBadgeView {
  status: string;
  label: string;
  tone: FeedBadgeTone;
}

export function feedBadgeView(
  health: FeedHealthEvent | null,
  now: number,
  live: boolean | null,
  connection: ConnectionState,
  locale: Locale = getLocale(),
): FeedBadgeView {
  if (browserDisconnected(live, connection)) {
    return {
      status: "DISCONNECTED",
      label: tFor(locale, "连接断开 · 重连中", "Disconnected · Reconnecting"),
      tone: "disconnected",
    };
  }
  if (health === null) {
    return live === false
      ? { status: "OFFLINE", label: tFor(locale, "无实时流", "No live stream"), tone: "offline" }
      : {
          status: "WAITING",
          label: tFor(locale, "等待行情", "Waiting for market data"),
          tone: "waiting",
        };
  }
  return {
    status: health.status,
    label: healthLabel(health, now, locale),
    tone: healthTone(health.status),
  };
}

export interface FeedAlertView {
  title: string;
  detail: string;
}

export function feedAlertView(
  health: FeedHealthEvent | null,
  now: number,
  live: boolean | null,
  connection: ConnectionState,
  locale: Locale = getLocale(),
): FeedAlertView | null {
  if (browserDisconnected(live, connection)) {
    return {
      title: tFor(locale, "连接已断开", "Connection lost"),
      detail: tFor(
        locale,
        "与本地守护进程的连接已断开，正在重连；图表保留最后一次快照。",
        "The connection to the local daemon was lost and is reconnecting. The chart keeps the last snapshot.",
      ),
    };
  }
  if (health === null || health.status !== "STALE") {
    return null;
  }
  const seconds = Math.floor(staleDurationMs(health, now) / 1000);
  return {
    title: tFor(locale, "实时行情断流", "Live market data interrupted"),
    detail: tFor(
      locale,
      `已持续 ${seconds} 秒，正在等待重连与回填；断流期间的 K 线会以 FEED_GAP 留空，不参与指标计算。`,
      `No data for ${seconds}s; waiting to reconnect and backfill. Candles during the gap stay empty as FEED_GAP and are excluded from indicator calculations.`,
    ),
  };
}
