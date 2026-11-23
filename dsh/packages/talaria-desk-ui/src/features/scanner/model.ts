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

export const SCANNER_KIND_ORDER: ScannerConditionKind[] = [
  "LIQUIDITY_ANOMALY",
  "VOLATILITY_EXPANSION",
  "FUNDING_EXTREME",
  "OI_DIVERGENCE",
  "KEY_LEVEL_EVENT",
  "RULE",
];

export function scannerSeverityLabel(severity: ScannerSeverity): string {
  switch (severity) {
    case "INFO": return t("提示", "Info");
    case "WARNING": return t("关注", "Watch");
    case "CRITICAL": return t("强异动", "Strong move");
  }
}

export const SCANNER_SEVERITY_RANK: Record<ScannerSeverity, number> = {
  INFO: 0,
  WARNING: 1,
  CRITICAL: 2,
};

const SUMMARY_LIMIT = 60;

/** 摘要或标签超过卡片一两行的容量时折叠，避免多指标异动撑满工作台。 */
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

/** 卡片标签：类型优先，再合并服务端的指标标签，去重且保持稳定顺序。 */
export function triggerTags(trigger: ScannerTrigger): string[] {
  const tags = [scannerKindLabel(trigger.kind), ...(trigger.tags ?? [])];
  return [...new Set(tags.filter((tag) => tag.trim().length > 0))];
}

function evidenceMetricLabel(metric: string): string {
  switch (metric) {
    case "zscore": return "z-score";
    case "quantile": return t("分位", "quantile");
    case "ratio": return t("倍数", "ratio");
    case "value": return t("值", "value");
    case "true_range": return t("真实波幅", "true range");
    case "atr": return "ATR";
    case "funding_rate": return t("资金费率", "Funding rate");
    case "settlements": return t("结算数", "Settlements");
    case "price_change": return t("价格变化", "Price change");
    case "oi_change": return t("OI 变化", "OI change");
    case "oi_points": return t("OI 点数", "OI points");
    case "level_price": return t("关键位", "Key level");
    case "strength": return t("强度", "Strength");
    case "distance": return t("距离", "Distance");
    case "breakout": return t("突破", "Breakout");
    default: return metric;
  }
}

function evidenceValue(metric: string, raw: string): string {
  const value = Number(raw);
  if (!Number.isFinite(value)) {
    return raw;
  }
  if (metric === "quantile") {
    return `${(value * 100).toFixed(0)}%`;
  }
  if (metric === "distance") {
    return `${(value * 100).toFixed(2)}%`;
  }
  if (metric === "breakout") {
    return value > 0 ? t("是", "Yes") : t("否", "No");
  }
  if (Math.abs(value) >= 1000) {
    return value.toFixed(0);
  }
  return value.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
}

export function evidenceLabel(evidence: ScannerEvidence): string {
  return `${evidenceMetricLabel(evidence.metric)} ${evidenceValue(
    evidence.metric,
    evidence.value,
  )}`;
}

export function triggerTime(ts: number, now = Date.now()): string {
  const delta = Math.max(0, now - ts);
  if (delta < 5_000) {
    return t("刚刚", "just now");
  }
  if (delta < 60_000) {
    return t(`${Math.floor(delta / 1_000)} 秒前`, `${Math.floor(delta / 1_000)}s ago`);
  }
  if (delta < 3_600_000) {
    return t(`${Math.floor(delta / 60_000)} 分钟前`, `${Math.floor(delta / 60_000)}m ago`);
  }
  if (delta < 86_400_000) {
    return t(`${Math.floor(delta / 3_600_000)} 小时前`, `${Math.floor(delta / 3_600_000)}h ago`);
  }
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

/** 点击卡片必须把主图切到触发标的/周期，并把快照截止设在触发时刻。 */
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
  if (!item.last_timeframe) {
    return null;
  }
  return {
    instrument_id: item.instrument_id,
    timeframe: item.last_timeframe,
    triggered_ts: item.last_trigger_ts,
  };
}

/** 同一类型在同一标的/周期内只显示一条，轮询与 WS 双通道不会重复。 */
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

export function severityView(severity: ScannerSeverity): {
  label: string;
  className: string;
} {
  return {
    label: scannerSeverityLabel(severity),
    className: `scanner-card severity-${severity.toLowerCase()}`,
  };
}
