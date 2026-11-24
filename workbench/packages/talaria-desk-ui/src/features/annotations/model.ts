import type {
  AgentAnnotationRecord,
  AnnotationEvidence,
  AnnotationSessionSummary,
  AnnotationStatus,
  DecimalString,
} from "@talaria/contracts";

import { getLocale, t, type Locale } from "../../lib/i18n";

export const ANNOTATION_BADGE = "智能体";
export const ANNOTATION_BADGE_EN = "Agent";

export function annotationBadge(locale: Locale = getLocale()): string {
  return locale === "zh" ? ANNOTATION_BADGE : ANNOTATION_BADGE_EN;
}

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

export const ANNOTATION_STATUS_LABEL_EN: Record<AnnotationStatus, string> = {
  ACTIVE: "Active",
  ADOPTED: "Adopted",
  DISMISSED: "Dismissed",
  ARCHIVED: "Archived",
};

export function annotationStatusLabel(
  status: AnnotationStatus,
  locale: Locale = getLocale(),
): string {
  return (locale === "zh" ? ANNOTATION_STATUS_LABEL : ANNOTATION_STATUS_LABEL_EN)[status];
}

const KIND_LABEL: Record<AgentAnnotationRecord["kind"], string> = {
  LEVEL: "水平线",
  ZONE: "价格带",
};

const KIND_LABEL_EN: Record<AgentAnnotationRecord["kind"], string> = {
  LEVEL: "Horizontal line",
  ZONE: "Price zone",
};

export function isActiveAnnotation(annotation: AgentAnnotationRecord): boolean {
  return annotation.status === "ACTIVE";
}

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

export function evidenceText(
  evidence: AnnotationEvidence,
  locale: Locale = getLocale(),
): string {
  if (typeof evidence.level_id === "string" && evidence.level_id.length > 0) {
    return t(`关键位 ${evidence.level_id}`, `Key level ${evidence.level_id}`);
  }
  if (typeof evidence.kline_ts === "number") {
    const time = new Date(evidence.kline_ts);
    const formatted = time.toLocaleString(locale === "zh" ? "zh-CN" : "en-US", { hour12: false });
    return t(`K 线 ${formatted}`, `Candle ${formatted}`);
  }
  return t("未标注证据", "Unlabeled evidence");
}

export function evidenceLines(
  annotation: AgentAnnotationRecord,
  locale: Locale = getLocale(),
): string[] {
  return annotation.evidence_refs.map((evidence) => {
    const base = evidenceText(evidence, locale);
    const note = evidence.note ?? "";
    return note.length > 0 ? t(`${base}（${note}）`, `${base} (${note})`) : base;
  });
}

export function kindLabel(
  kind: AgentAnnotationRecord["kind"],
  locale: Locale = getLocale(),
): string {
  return (locale === "zh" ? KIND_LABEL : KIND_LABEL_EN)[kind];
}

function decimalText(value: DecimalString): string {
  return String(value);
}

export function annotationPriceLabel(annotation: AgentAnnotationRecord): string {
  if (annotation.kind === "LEVEL") return decimalText(annotation.price_low);
  return `${decimalText(annotation.price_low)}–${decimalText(annotation.price_high)}`;
}

export function annotationSummary(
  annotation: AgentAnnotationRecord,
  locale: Locale = getLocale(),
): string {
  return `${kindLabel(annotation.kind, locale)} ${annotationPriceLabel(annotation)} · ${annotationBadge(locale)}`;
}

export function annotationZoneEnd(
  annotation: AgentAnnotationRecord,
  timeframeMsValue: number,
): number {
  return annotation.ts_anchor + timeframeMsValue * 100;
}
