import type {
  ScannerConditionKind,
  ScannerEvidence,
  ScannerFocusItem,
  ScannerSeverity,
  ScannerTrigger,
} from "@talaria/contracts";

import { getLocale, t } from "../../lib/i18n";

export function scannerKindLabel(kind: ScannerConditionKind): string {
  switch (kind) {
    case "LIQUIDITY_ANOMALY": return t("流动性异常", "Liquidity anomaly");
    case "VOLATILITY_EXPANSION": return t("波动率扩张", "Volatility expansion");
    case "FUNDING_EXTREME": return t("资金费率极端", "Funding extreme");
    case "OI_DIVERGENCE": return t("OI 背离", "OI divergence");
    case "KEY_LEVEL_EVENT": return t("关键位事件", "Key level event");
    case "RULE": return t("规则命中", "Rule hit");
  }
}

export function scannerSeverityLabel(severity: ScannerSeverity): string {
  switch (severity) {
    case "INFO": return t("提示", "Info");
    case "WARNING": return t("关注", "Watch");
    case "CRITICAL": return t("强异动", "Critical");
  }
}

export const SCANNER_SEVERITY_RANK: Record<ScannerSeverity, number> = {
  INFO: 0,
  WARNING: 1,
  CRITICAL: 2,
};

const SUMMARY_LIMIT = 60;

export function summaryNeedsCollapse(
  trigger: Pick<ScannerTrigger, "summary" | "tags">,
): boolean {
  return trigger.summary.length > SUMMARY_LIMIT || (trigger.tags?.length ?? 0) > 4;
}

export function summaryPreview(summary: string): string {
  return summary.length > SUMMARY_LIMIT
    ? `${summary.slice(0, SUMMARY_LIMIT).trimEnd()}…`
    : summary;
}

export function triggerTags(trigger: ScannerTrigger): string[] {
  const tags = [scannerKindLabel(trigger.kind), ...(trigger.tags ?? [])];
  return [...new Set(tags.filter((tag) => tag.trim().length > 0))];
}

export function evidenceLabel(evidence: ScannerEvidence): string {
  return `${evidence.metric} ${evidence.value}`;
}

export function triggerTime(ts: number, now = Date.now()): string {
  const delta = Math.max(0, now - ts);
  if (delta < 5_000) return t("刚刚", "just now");
  if (delta < 60_000) return t(`${Math.floor(delta / 1_000)} 秒前`, `${Math.floor(delta / 1_000)}s ago`);
  if (delta < 3_600_000) return t(`${Math.floor(delta / 60_000)} 分钟前`, `${Math.floor(delta / 60_000)}m ago`);
  if (delta < 86_400_000) return t(`${Math.floor(delta / 3_600_000)} 小时前`, `${Math.floor(delta / 3_600_000)}h ago`);
  return new Intl.DateTimeFormat(getLocale() === "zh" ? "zh-CN" : "en-US", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(ts));
}

export interface ScannerLocatePayload {
  instrument_id: string;
  timeframe: string;
  triggered_ts: number;
}

export function locatePayload(trigger: ScannerTrigger): ScannerLocatePayload {
  return {
    instrument_id: trigger.instrument_id,
    timeframe: trigger.timeframe,
    triggered_ts: trigger.created_ts,
  };
}

export function focusLocatePayload(
  item: ScannerFocusItem,
): ScannerLocatePayload | null {
  if (!item.last_timeframe) return null;
  return {
    instrument_id: item.instrument_id,
    timeframe: item.last_timeframe,
    triggered_ts: item.last_trigger_ts,
  };
}

export function mergeTrigger(
  current: ScannerTrigger[],
  incoming: ScannerTrigger,
  limit = 50,
): ScannerTrigger[] {
  if (current.some((item) => item.trigger_id === incoming.trigger_id)) {
    return current;
  }
  return [incoming, ...current].slice(0, limit);
}

export function sortTriggers(triggers: ScannerTrigger[]): ScannerTrigger[] {
  return [...triggers].sort(
    (left, right) =>
      right.created_ts - left.created_ts ||
      left.trigger_id.localeCompare(right.trigger_id),
  );
}
