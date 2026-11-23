import type {
  Abstain,
  AbstainReason,
  ConsultationRecord,
  EvidenceRef,
  SetupInsight,
  UsageSummary,
} from "@talaria/contracts";

import { getLocale, type Locale } from "../../lib/i18n";

/** Abstain 的四类主因 + 两类宿主/格式兜底；前端只做降级展示，不臆测结论。 */
const ABSTAIN_COPY: Record<AbstainReason, { title: string; hint: string }> = {
  STALE_SNAPSHOT: {
    title: "快照已过期",
    hint: "冻结时点超过允许窗口，请刷新快照后重新会诊。",
  },
  EVIDENCE_CONFLICT: {
    title: "关键证据冲突",
    hint: "工具结果与画线/关键位互相矛盾，本次不输出结论。",
  },
  GEOMETRY_INVERTED: {
    title: "几何关系倒挂",
    hint: "止损或止盈方向与交易方向相反，请检查入场带。",
  },
  MODEL_TIMEOUT: {
    title: "推理超时",
    hint: "协同宿主未在时限内返回，图表与画线不受影响。",
  },
  HOST_OFFLINE: {
    title: "协同宿主离线",
    hint: "快通道仍可用；可稍后重试，或继续本地分析。",
  },
  MODEL_INVALID_OUTPUT: {
    title: "输出无法核对",
    hint: "宿主返回的内容不符合 Setup Insight 契约，已拒绝展示。",
  },
};

const ABSTAIN_COPY_EN: Record<AbstainReason, { title: string; hint: string }> = {
  STALE_SNAPSHOT: {
    title: "Snapshot expired",
    hint: "The frozen timestamp is outside the allowed window. Refresh the snapshot and run the consultation again.",
  },
  EVIDENCE_CONFLICT: {
    title: "Conflicting evidence",
    hint: "Tool results contradict the drawings or key levels, so no conclusion is produced this time.",
  },
  GEOMETRY_INVERTED: {
    title: "Inverted geometry",
    hint: "The stop or target points against the trade direction. Check the entry zone.",
  },
  MODEL_TIMEOUT: {
    title: "Reasoning timed out",
    hint: "The collaboration host did not respond in time. The chart and drawings are unaffected.",
  },
  HOST_OFFLINE: {
    title: "Collaboration host offline",
    hint: "The fast channel still works; retry later or continue local analysis.",
  },
  MODEL_INVALID_OUTPUT: {
    title: "Unverifiable output",
    hint: "The host response did not match the Setup Insight contract and was rejected.",
  },
};

export function abstainCopy(
  abstain: Abstain,
  locale: Locale = getLocale(),
): { title: string; hint: string } {
  const copy = locale === "zh" ? ABSTAIN_COPY : ABSTAIN_COPY_EN;
  return copy[abstain.reason_code] ?? {
    title: locale === "zh" ? "本次会诊已放弃" : "This consultation was abandoned",
    hint: abstain.message,
  };
}

export function reasonTitle(reason: AbstainReason, locale: Locale = getLocale()): string {
  const copy = locale === "zh" ? ABSTAIN_COPY : ABSTAIN_COPY_EN;
  return copy[reason]?.title ?? reason;
}

export function consultationStatusLabel(
  record: ConsultationRecord,
  locale: Locale = getLocale(),
): string {
  if (record.status === "PENDING") {
    return locale === "zh" ? "等待协同宿主" : "Waiting for collaboration host";
  }
  if (record.status === "READY") return "Setup Insight";
  if (record.status === "ABSTAIN") {
    return locale === "zh" ? "已放弃（Abstain）" : "Abstained";
  }
  return locale === "zh" ? "会诊失败" : "Consultation failed";
}

export function directionLabel(
  direction: SetupInsight["direction"],
  locale: Locale = getLocale(),
): string {
  if (direction === "LONG") return locale === "zh" ? "偏多观察" : "Bullish watch";
  if (direction === "SHORT") return locale === "zh" ? "偏空观察" : "Bearish watch";
  return locale === "zh" ? "中性观察" : "Neutral watch";
}

export function evidenceLabel(ref: EvidenceRef): string {
  return ref.price_low === null || ref.price_low === undefined
    ? ref.label
    : `${ref.label} · ${ref.price_low}–${ref.price_high}`;
}

export interface EvidenceGroup {
  kind: string;
  label: string;
  items: EvidenceRef[];
}

const KIND_LABELS: Record<string, string> = {
  SNAPSHOT: "快照",
  CHART_VIEW: "图表视窗",
  KEY_LEVEL: "关键位",
  INDICATOR: "指标",
  OPERATOR: "算子",
  HUMAN_DRAWING: "人工画线",
};

const KIND_LABELS_EN: Record<string, string> = {
  SNAPSHOT: "Snapshot",
  CHART_VIEW: "Chart view",
  KEY_LEVEL: "Key level",
  INDICATOR: "Indicator",
  OPERATOR: "Operator",
  HUMAN_DRAWING: "Manual drawing",
};

/** 证据按来源折叠；同一 evidence_id 只保留一条，避免模型重复引用。 */
export function groupEvidence(
  refs: readonly EvidenceRef[],
  locale: Locale = getLocale(),
): EvidenceGroup[] {
  const seen = new Set<string>();
  const groups = new Map<string, EvidenceGroup>();
  for (const ref of refs) {
    if (seen.has(ref.evidence_id)) continue;
    seen.add(ref.evidence_id);
    const group = groups.get(ref.kind) ?? {
      kind: ref.kind,
      label: (locale === "zh" ? KIND_LABELS : KIND_LABELS_EN)[ref.kind] ?? ref.kind,
      items: [],
    };
    group.items.push(ref);
    groups.set(ref.kind, group);
  }
  return [...groups.values()];
}

export function evidenceCount(refs: readonly EvidenceRef[] | undefined): number {
  return new Set((refs ?? []).map((ref) => ref.evidence_id)).size;
}

export function usageLine(
  summary: UsageSummary | null,
  locale: Locale = getLocale(),
): string {
  if (summary === null) {
    return locale === "zh" ? "Token 用量暂不可用" : "Token usage unavailable";
  }
  const tag = locale === "zh" ? "zh-CN" : "en-US";
  const input = (summary.input_tokens ?? 0).toLocaleString(tag);
  const cached = (summary.cached_input_tokens ?? 0).toLocaleString(tag);
  const output = (summary.output_tokens ?? 0).toLocaleString(tag);
  const turns = summary.turns ?? 0;
  const missing = summary.missing_reports ?? 0;
  if (locale === "zh") {
    const suffix = missing > 0 ? ` · 未报 ${missing} 轮` : "";
    return `输入 ${input} · 缓存命中 ${cached} · 输出 ${output} · ${turns} 轮${suffix}`;
  }
  const suffix = missing > 0 ? ` · ${missing} unreported` : "";
  return `Input ${input} · Cache hit ${cached} · Output ${output} · ${turns} turns${suffix}`;
}

export function isPending(record: ConsultationRecord): boolean {
  return record.status === "PENDING";
}
