import type { BriefingStatus, ScannerBriefing } from "@talaria/contracts";

import { getLocale, t, type Locale } from "../../lib/i18n";

export const BRIEFING_STATUS_LABELS: Record<BriefingStatus, string> = {
  PENDING: "生成中",
  READY: "已就绪",
  ABSTAIN: "Abstain",
  FAILED: "失败",
};

export const BRIEFING_STATUS_LABELS_EN: Record<BriefingStatus, string> = {
  PENDING: "Generating",
  READY: "Ready",
  ABSTAIN: "Abstain",
  FAILED: "Failed",
};

export const BRIEFING_STATUS_TONE: Record<BriefingStatus, string> = {
  PENDING: "pending",
  READY: "ready",
  ABSTAIN: "abstain",
  FAILED: "failed",
};

const ABSTAIN_LABELS: Record<string, string> = {
  STALE_SNAPSHOT: "快照已过期，拒绝基于旧数据给出观点",
  EVIDENCE_CONFLICT: "证据不足或互相冲突，不输出伪结论",
  GEOMETRY_INVERTED: "可见区间价格上下界倒置",
  MODEL_TIMEOUT: "宿主模型超时，已中止本轮",
  HOST_OFFLINE: "协同宿主离线或处于 --solo 模式，仅保留数值异动卡",
  MODEL_INVALID_OUTPUT: "宿主返回格式不合法，无法解析为简报",
};

const ABSTAIN_LABELS_EN: Record<string, string> = {
  STALE_SNAPSHOT: "The snapshot expired; no view is given from stale data",
  EVIDENCE_CONFLICT: "Insufficient or conflicting evidence; no false conclusion is produced",
  GEOMETRY_INVERTED: "The visible price range is inverted",
  MODEL_TIMEOUT: "The host model timed out; this round was aborted",
  HOST_OFFLINE: "The collaboration host is offline or in --solo mode; only the numeric move card is kept",
  MODEL_INVALID_OUTPUT: "The host returned an invalid format that cannot be parsed into a briefing",
};

const COLLAPSE_LIMIT = 120;

export function briefingStatusLabel(
  status: BriefingStatus,
  locale: Locale = getLocale(),
): string {
  return (locale === "zh" ? BRIEFING_STATUS_LABELS : BRIEFING_STATUS_LABELS_EN)[status];
}

export function briefingStatusTone(status: BriefingStatus): string {
  return BRIEFING_STATUS_TONE[status];
}

export function abstainReasonLabel(
  reason: string | null | undefined,
  locale: Locale = getLocale(),
): string {
  if (!reason) return t("没有形成可行动观点", "No actionable view was formed");
  const labels = locale === "zh" ? ABSTAIN_LABELS : ABSTAIN_LABELS_EN;
  return labels[reason] ?? t(`Abstain：${reason}`, `Abstain: ${reason}`);
}

export function briefingTitle(
  briefing: ScannerBriefing,
  locale: Locale = getLocale(),
): string {
  if (briefing.status === "READY") {
    return t(
      `${briefing.instrument_id} · ${briefing.timeframe} 异动简报`,
      `${briefing.instrument_id} · ${briefing.timeframe} move briefing`,
    );
  }
  if (briefing.status === "ABSTAIN") {
    return t("本轮没有有效观点", "No actionable view this round");
  }
  if (briefing.status === "FAILED") return t("简报生成失败", "Briefing generation failed");
  return t("正在读取冻结快照…", "Reading the frozen snapshot…");
}

export function briefingNeedsCollapse(briefing: ScannerBriefing): boolean {
  const text = briefing.summary?.trigger_reason ?? "";
  return text.length > COLLAPSE_LIMIT;
}

export function briefingPreview(text: string, limit = COLLAPSE_LIMIT): string {
  if (text.length <= limit) return text;
  return `${text.slice(0, limit)}…`;
}

export function briefingQuestion(
  briefing: ScannerBriefing,
  locale: Locale = getLocale(),
): string {
  const reason = briefing.summary?.trigger_reason
    ?? t("这次数值异动", "this numeric move");
  return t(
    `请结合 ${briefing.instrument_id} ${briefing.timeframe} 的冻结快照 ${briefing.snapshot_id}，解读「${reason}」，并给出关键价位、风险与证据引用。`,
    `Using the frozen snapshot ${briefing.snapshot_id} of ${briefing.instrument_id} ${briefing.timeframe}, interpret “${reason}” and provide key levels, risks, and evidence references.`,
  );
}

export function briefingLocate(briefing: ScannerBriefing): {
  instrument_id: string;
  timeframe: string;
  triggered_ts: number;
} {
  return {
    instrument_id: briefing.instrument_id,
    timeframe: briefing.timeframe,
    triggered_ts: briefing.created_ts,
  };
}

export function briefingTime(
  timestamp: number,
  now: number,
  locale: Locale = getLocale(),
): string {
  if (!Number.isFinite(timestamp) || timestamp <= 0) {
    return t("时间未知", "Time unknown");
  }
  const diff = Math.max(0, now - timestamp);
  if (diff < 60_000) return t("刚刚", "Just now");
  if (diff < 3_600_000) {
    const minutes = Math.floor(diff / 60_000);
    return t(`${minutes} 分钟前`, `${minutes}m ago`);
  }
  if (diff < 86_400_000) {
    const hours = Math.floor(diff / 3_600_000);
    return t(`${hours} 小时前`, `${hours}h ago`);
  }
  return new Intl.DateTimeFormat(locale === "zh" ? "zh-CN" : "en-US", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(timestamp));
}
