import type { FeedHealthEvent } from "@talaria/contracts";

export type FeedTone = "live" | "recovering" | "stale";

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

/** 把毫秒时长写成简体中文的“N 秒 / N 分 M 秒”。 */
export function formatDuration(ms: number): string {
  const seconds = Math.floor(Math.max(ms, 0) / 1000);
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
): string {
  if (health === null) {
    return "等待行情";
  }
  if (health.status === "LIVE") {
    return "实时";
  }
  if (health.status === "RECOVERING") {
    return "恢复中";
  }
  return `断流 ${formatDuration(staleDurationMs(health, now))}`;
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
