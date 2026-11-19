import type {
  Abstain,
  AbstainReason,
  ConsultationRecord,
  EvidenceRef,
  SetupInsight,
  UsageSummary,
} from "@talaria/contracts";

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

export function abstainCopy(abstain: Abstain): { title: string; hint: string } {
  return ABSTAIN_COPY[abstain.reason_code] ?? {
    title: "本次会诊已放弃",
    hint: abstain.message,
  };
}

export function reasonTitle(reason: AbstainReason): string {
  return ABSTAIN_COPY[reason]?.title ?? reason;
}

export function consultationStatusLabel(record: ConsultationRecord): string {
  if (record.status === "PENDING") return "等待协同宿主";
  if (record.status === "READY") return "Setup Insight";
  if (record.status === "ABSTAIN") return "已放弃（Abstain）";
  return "会诊失败";
}

export function directionLabel(direction: SetupInsight["direction"]): string {
  if (direction === "LONG") return "偏多观察";
  if (direction === "SHORT") return "偏空观察";
  return "中性观察";
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

/** 证据按来源折叠；同一 evidence_id 只保留一条，避免模型重复引用。 */
export function groupEvidence(refs: readonly EvidenceRef[]): EvidenceGroup[] {
  const seen = new Set<string>();
  const groups = new Map<string, EvidenceGroup>();
  for (const ref of refs) {
    if (seen.has(ref.evidence_id)) continue;
    seen.add(ref.evidence_id);
    const group = groups.get(ref.kind) ?? {
      kind: ref.kind,
      label: KIND_LABELS[ref.kind] ?? ref.kind,
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

export function usageLine(summary: UsageSummary | null): string {
  if (summary === null) return "Token 用量暂不可用";
  const input = (summary.input_tokens ?? 0).toLocaleString("zh-CN");
  const cached = (summary.cached_input_tokens ?? 0).toLocaleString("zh-CN");
  const output = (summary.output_tokens ?? 0).toLocaleString("zh-CN");
  const turns = summary.turns ?? 0;
  const missing = summary.missing_reports ?? 0;
  const suffix = missing > 0 ? ` · 未报 ${missing} 轮` : "";
  return `输入 ${input} · 缓存命中 ${cached} · 输出 ${output} · ${turns} 轮${suffix}`;
}

export function isPending(record: ConsultationRecord): boolean {
  return record.status === "PENDING";
}
