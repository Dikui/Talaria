import type { FeedHealthEvent } from "@talaria/contracts";

import { getLocale, t, type Locale } from "../../lib/i18n";

import type { ConnectionState } from "./reducer";

export type FeedTone = "live" | "recovering" | "stale";
export type FeedBadgeTone =
  | FeedTone
  | "offline"
  | "waiting"
  | "disconnected";

/** 服务端 12s 无活动即判 STALE，前端徽标用同一阈值解释断流时长。 */
export const STALE_AFTER_MS = 12_000;

export function staleDurationMs(
  health: FeedHealthEvent | null,
  now: number,
): number {
  if (health === null || health.status !== "STALE" || health.stale_since == null) {
    return 0;
  }
  return Math.max(now - health.stale_since, 0);
}

/** 把毫秒时长写成当前语言的“N 秒 / N 分 M 秒”。 */
export function formatDuration(ms: number, locale: Locale = getLocale()): string {
  const seconds = Math.floor(Math.max(ms, 0) / 1000);
  if (locale === "en") {
    if (seconds < 60) {
      return `${seconds}s`;
    }
    const minutes = Math.floor(seconds / 60);
    const rest = seconds % 60;
    return rest === 0 ? `${minutes}m` : `${minutes}m ${rest}s`;
  }
  if (seconds < 60) {
    return `${seconds} 秒`;
  }
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest === 0 ? `${minutes} 分` : `${minutes} 分 ${rest} 秒`;
}

export function healthTone(status: FeedHealthEvent["status"]): FeedTone {
  if (status === "LIVE") {
    return "live";
  }
  if (status === "RECOVERING") {
    return "recovering";
  }
  return "stale";
}

/** 顶栏徽标文案：LIVE=实时、RECOVERING=恢复中、STALE=断流 + 计时。 */
export function healthLabel(
  health: FeedHealthEvent | null,
  now: number,
  locale: Locale = getLocale(),
): string {
  if (health === null) {
    return t("等待行情", "Waiting for market data");
  }
  if (health.status === "LIVE") {
    return t("实时", "Live");
  }
  if (health.status === "RECOVERING") {
    return t("恢复中", "Recovering");
  }
  const duration = formatDuration(staleDurationMs(health, now), locale);
  return t(`断流 ${duration}`, `Stream stale ${duration}`);
}

/** 断流是否已超过 12s 判定窗口（由服务端判定，前端只做展示提示）。 */
export function isStaleWindowExceeded(
  health: FeedHealthEvent | null,
  now: number,
): boolean {
  return (
    health !== null &&
    health.status === "STALE" &&
    staleDurationMs(health, now) >= STALE_AFTER_MS
  );
}

/** 浏览器 WS 断开时，不能再用旧的 LIVE health 掩盖“本地连接已断”。 */
function browserDisconnected(
  live: boolean | null,
  connection: ConnectionState,
): boolean {
  if (live === false) {
    return false;
  }
  if (connection === "closed") {
    return true;
  }
  // 首次连接（尚未收到 hello）用“等待行情”，只有收到过 live hello 后的重连
  // 才显示“连接断开 · 重连中”。
  return connection === "connecting" && live === true;
}

export interface FeedBadgeView {
  status: string;
  label: string;
  tone: FeedBadgeTone;
}

/** 顶栏健康徽标的展示模型：连接状态优先于服务端 health。 */
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
      label: t("连接断开 · 重连中", "Disconnected · Reconnecting"),
      tone: "disconnected",
    };
  }
  if (health === null) {
    return live === false
      ? { status: "OFFLINE", label: t("无实时流", "No live stream"), tone: "offline" }
      : { status: "WAITING", label: t("等待行情", "Waiting for market data"), tone: "waiting" };
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

/** 提示条：本地连接断开优先于行情断流；两者都没有时返回 null。 */
export function feedAlertView(
  health: FeedHealthEvent | null,
  now: number,
  live: boolean | null,
  connection: ConnectionState,
  locale: Locale = getLocale(),
): FeedAlertView | null {
  if (browserDisconnected(live, connection)) {
    return {
      title: t("连接已断开", "Connection lost"),
      detail: t(
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
    title: t("实时行情断流", "Live market data interrupted"),
    detail: t(
      `已持续 ${seconds} 秒，正在等待重连与回填；断流期间的 K 线会以 FEED_GAP 留空，不参与指标计算。`,
      `No data for ${seconds}s; waiting to reconnect and backfill. Candles during the gap stay empty as FEED_GAP and are excluded from indicator calculations.`,
    ),
  };
}
