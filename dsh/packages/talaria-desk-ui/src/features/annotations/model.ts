import type {
  AgentAnnotationRecord,
  AnnotationEvidence,
  AnnotationSessionSummary,
  AnnotationStatus,
  DecimalString,
} from "@talaria/contracts";

export const ANNOTATION_BADGE = "智能体";

const UNIT_MS: Record<string, number> = {
  "1m": 60_000,
  "5m": 300_000,
  "15m": 900_000,
  "1h": 3_600_000,
  "4h": 14_400_000,
  "1d": 86_400_000,
};

export function timeframeMs(timeframe: string): number {
  return UNIT_MS[timeframe] ?? UNIT_MS["1m"];
}

export const ANNOTATION_STATUS_LABEL: Record<AnnotationStatus, string> = {
  ACTIVE: "活跃",
  ADOPTED: "已采纳",
  DISMISSED: "已忽略",
  ARCHIVED: "已归档",
};

const KIND_LABEL: Record<AgentAnnotationRecord["kind"], string> = {
  LEVEL: "水平线",
  ZONE: "价格带",
};

export function isActiveAnnotation(annotation: AgentAnnotationRecord): boolean {
  return annotation.status === "ACTIVE";
}

/** 图层只画当前仍待处理的建议；已采纳/已忽略的流水只保留在面板审计里。 */
export function activeAnnotations(
  annotations: readonly AgentAnnotationRecord[],
): AgentAnnotationRecord[] {
  return annotations.filter(isActiveAnnotation);
}

export function filterAnnotations(
  annotations: readonly AgentAnnotationRecord[],
  sessionId: string | null,
): AgentAnnotationRecord[] {
  if (sessionId === null || sessionId.length === 0) return [...annotations];
  return annotations.filter((annotation) => annotation.session_id === sessionId);
}

/** 与后端 `GET /api/annotations/sessions` 相同的展示口径：忽略已归档。 */
export function summarizeSessions(
  annotations: readonly AgentAnnotationRecord[],
): AnnotationSessionSummary[] {
  const grouped = new Map<string, AnnotationSessionSummary>();
  for (const annotation of annotations) {
    if (annotation.status === "ARCHIVED") continue;
    const current = grouped.get(annotation.session_id);
    if (current === undefined) {
      grouped.set(annotation.session_id, {
        session_id: annotation.session_id,
        instrument_ids: [annotation.instrument_id],
        annotation_count: 1,
        latest_ts: annotation.updated_ts,
      });
      continue;
    }
    current.annotation_count += 1;
    current.latest_ts = Math.max(current.latest_ts, annotation.updated_ts);
    if (!current.instrument_ids.includes(annotation.instrument_id)) {
      current.instrument_ids.push(annotation.instrument_id);
    }
  }
  return [...grouped.values()].sort((left, right) => right.latest_ts - left.latest_ts);
}

export function evidenceText(evidence: AnnotationEvidence): string {
  if (typeof evidence.level_id === "string" && evidence.level_id.length > 0) {
    return `关键位 ${evidence.level_id}`;
  }
  if (typeof evidence.kline_ts === "number") {
    const time = new Date(evidence.kline_ts);
    return `K 线 ${time.toLocaleString("zh-CN", { hour12: false })}`;
  }
  return "未标注证据";
}

export function evidenceLines(annotation: AgentAnnotationRecord): string[] {
  return annotation.evidence_refs.map((evidence) => {
    const base = evidenceText(evidence);
    const note = evidence.note ?? "";
    return note.length > 0 ? `${base}（${note}）` : base;
  });
}

export function annotationPopoverLines(annotation: AgentAnnotationRecord): string[] {
  return [
    `快照 ${annotation.snapshot_id}`,
    `第 ${annotation.round} 轮 · ${annotation.session_id}`,
    ...evidenceLines(annotation).map((line) => `证据：${line}`),
  ];
}

export function kindLabel(kind: AgentAnnotationRecord["kind"]): string {
  return KIND_LABEL[kind];
}

function decimalText(value: DecimalString): string {
  return String(value);
}

export function annotationPriceLabel(annotation: AgentAnnotationRecord): string {
  if (annotation.kind === "LEVEL") return decimalText(annotation.price_low);
  return `${decimalText(annotation.price_low)}–${decimalText(annotation.price_high)}`;
}

export function annotationSummary(annotation: AgentAnnotationRecord): string {
  return `${kindLabel(annotation.kind)} ${annotationPriceLabel(annotation)} · ${ANNOTATION_BADGE}`;
}

/** 价格带在时间轴上从锚点向前延伸 100 根；与采纳为矩形画线的跨度一致。 */
export function annotationZoneEnd(
  annotation: AgentAnnotationRecord,
  timeframeMs: number,
): number {
  return annotation.ts_anchor + timeframeMs * 100;
}

/** 390 只读：操作全部隐藏，但图层本身仍按 activeAnnotations 绘制。 */
export function canManageAnnotations(readOnly: boolean): boolean {
  return !readOnly;
}
