import type {
  ScannerConditionKind,
  ScannerEvidence,
  ScannerFocusItem,
  ScannerSeverity,
  ScannerTrigger,
} from "@talaria/contracts";

export const SCANNER_KIND_LABELS: Record<ScannerConditionKind, string> = {
  LIQUIDITY_ANOMALY: "流动性异常",
  VOLATILITY_EXPANSION: "波动率扩张",
  FUNDING_EXTREME: "资金费率极端",
  OI_DIVERGENCE: "OI 背离",
  KEY_LEVEL_EVENT: "关键位事件",
  RULE: "规则命中",
};

export const SCANNER_KIND_ORDER: ScannerConditionKind[] = [
  "LIQUIDITY_ANOMALY",
  "VOLATILITY_EXPANSION",
  "FUNDING_EXTREME",
  "OI_DIVERGENCE",
  "KEY_LEVEL_EVENT",
  "RULE",
];

export const SCANNER_SEVERITY_LABELS: Record<ScannerSeverity, string> = {
  INFO: "提示",
  WARNING: "关注",
  CRITICAL: "强异动",
};

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
  const tags = [SCANNER_KIND_LABELS[trigger.kind], ...(trigger.tags ?? [])];
  return [...new Set(tags.filter((tag) => tag.trim().length > 0))];
}

const EVIDENCE_LABELS: Record<string, string> = {
  zscore: "z-score",
  quantile: "分位",
  ratio: "倍数",
  value: "值",
  true_range: "真实波幅",
  atr: "ATR",
  funding_rate: "资金费率",
  settlements: "结算数",
  price_change: "价格变化",
  oi_change: "OI 变化",
  oi_points: "OI 点数",
  level_price: "关键位",
  strength: "强度",
  distance: "距离",
  breakout: "突破",
};

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
    return value > 0 ? "是" : "否";
  }
  if (Math.abs(value) >= 1000) {
    return value.toFixed(0);
  }
  return value.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
}

export function evidenceLabel(evidence: ScannerEvidence): string {
  return `${EVIDENCE_LABELS[evidence.metric] ?? evidence.metric} ${evidenceValue(
    evidence.metric,
    evidence.value,
  )}`;
}

export function triggerTime(ts: number, now = Date.now()): string {
  const delta = Math.max(0, now - ts);
  if (delta < 5_000) {
    return "刚刚";
  }
  if (delta < 60_000) {
    return `${Math.floor(delta / 1_000)} 秒前`;
  }
  if (delta < 3_600_000) {
    return `${Math.floor(delta / 60_000)} 分钟前`;
  }
  if (delta < 86_400_000) {
    return `${Math.floor(delta / 3_600_000)} 小时前`;
  }
  return new Intl.DateTimeFormat("zh-CN", {
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
    label: SCANNER_SEVERITY_LABELS[severity],
    className: `scanner-card severity-${severity.toLowerCase()}`,
  };
}
