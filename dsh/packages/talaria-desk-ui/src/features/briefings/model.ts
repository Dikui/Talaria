import type { BriefingStatus, ScannerBriefing } from "@talaria/contracts";

export const BRIEFING_STATUS_LABELS: Record<BriefingStatus, string> = {
  PENDING: "生成中",
  READY: "已就绪",
  ABSTAIN: "Abstain",
  FAILED: "失败",
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

const COLLAPSE_LIMIT = 120;

export function briefingStatusLabel(status: BriefingStatus): string {
  return BRIEFING_STATUS_LABELS[status];
}

export function briefingStatusTone(status: BriefingStatus): string {
  return BRIEFING_STATUS_TONE[status];
}

export function abstainReasonLabel(reason: string | null | undefined): string {
  if (!reason) return "没有形成可行动观点";
  return ABSTAIN_LABELS[reason] ?? `Abstain：${reason}`;
}

export function briefingTitle(briefing: ScannerBriefing): string {
  if (briefing.status === "READY") {
    return `${briefing.instrument_id} · ${briefing.timeframe} 异动简报`;
  }
  if (briefing.status === "ABSTAIN") return "本轮没有有效观点";
  if (briefing.status === "FAILED") return "简报生成失败";
  return "正在读取冻结快照…";
}

export function briefingNeedsCollapse(briefing: ScannerBriefing): boolean {
  const text = briefing.summary?.trigger_reason ?? "";
  return text.length > COLLAPSE_LIMIT;
}

export function briefingPreview(text: string, limit = COLLAPSE_LIMIT): string {
  if (text.length <= limit) return text;
  return `${text.slice(0, limit)}…`;
}

export function briefingQuestion(briefing: ScannerBriefing): string {
  const reason = briefing.summary?.trigger_reason ?? "这次数值异动";
  return `请结合 ${briefing.instrument_id} ${briefing.timeframe} 的冻结快照 ${briefing.snapshot_id}，解读「${reason}」，并给出关键价位、风险与证据引用。`;
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

export function briefingTime(timestamp: number, now: number): string {
  if (!Number.isFinite(timestamp) || timestamp <= 0) return "时间未知";
  const diff = Math.max(0, now - timestamp);
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(timestamp));
}
