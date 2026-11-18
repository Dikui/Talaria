import type {
  AlertCondition,
  AlertConditionKind,
  AlertCreate,
  AlertDirection,
  AlertMetric,
  AlertOverlayMode,
  AlertRecord,
  AlertStatus,
  AlertTriggerRecord,
  DecimalString,
  DrawingKind,
  IndicatorName,
} from "@talaria/contracts";

import { KIND_LABELS } from "../drawings/tools";
import { INDICATOR_LABELS } from "../indicators/config";

/** 规则列表的四个页签；DISABLED 与 EXPIRED 都归入“已结束”。 */
export const ALERT_TABS = [
  { id: "active", label: "活跃" },
  { id: "cooling", label: "冷却" },
  { id: "expired", label: "已过期" },
  { id: "draft", label: "草案" },
] as const;

export type AlertTabId = (typeof ALERT_TABS)[number]["id"];

export const ALERT_STATUS_LABELS: Record<AlertStatus, string> = {
  ARMED: "待命",
  COOLDOWN: "冷却中",
  EXPIRED: "已过期",
  DISABLED: "已停用",
  DRAFT: "草案",
};

export const METRIC_LABELS: Record<AlertMetric, string> = {
  last_price: "最新价",
  bid_price: "买一价",
  ask_price: "卖一价",
  mark_price: "标记价格",
  funding_rate: "资金费率",
  open_interest: "持仓量",
};

/** 与后端一致：衍生品指标暂不支持预警，表单也不展示。 */
export const ALERT_INDICATORS: IndicatorName[] = [
  "EMA",
  "SMA",
  "BOLL",
  "VWAP",
  "RSI",
  "ATR",
];

export const ALERT_OVERLAY_KINDS: DrawingKind[] = [
  "HORIZONTAL_LINE",
  "RAY",
  "SEGMENT",
  "PARALLEL_CHANNEL",
  "FIB_RETRACEMENT",
  "RECTANGLE",
];

export function statusToTab(status: AlertStatus): AlertTabId {
  switch (status) {
    case "ARMED":
      return "active";
    case "COOLDOWN":
      return "cooling";
    case "DRAFT":
      return "draft";
    case "EXPIRED":
    case "DISABLED":
      return "expired";
  }
}

export function alertsForTab(
  alerts: AlertRecord[],
  tab: AlertTabId,
): AlertRecord[] {
  return alerts
    .filter((alert) => statusToTab(alert.status) === tab)
    .slice()
    .sort(compareAlerts);
}

/** 新编辑的规则排前面；同一秒内用 id 保证确定性。 */
export function compareAlerts(left: AlertRecord, right: AlertRecord): number {
  if (right.updated_ts !== left.updated_ts) {
    return right.updated_ts - left.updated_ts;
  }
  return left.alert_id.localeCompare(right.alert_id);
}

export function tabCounts(alerts: AlertRecord[]): Record<AlertTabId, number> {
  const counts: Record<AlertTabId, number> = {
    active: 0,
    cooling: 0,
    expired: 0,
    draft: 0,
  };
  for (const alert of alerts) {
    counts[statusToTab(alert.status)] += 1;
  }
  return counts;
}

/** 相对时间只做展示；负偏差夹到“刚刚”，避免时钟漂移显示未来。 */
export function relativeTime(ts: number, now: number): string {
  const delta = Math.max(0, now - ts);
  if (delta < 5_000) {
    return "刚刚";
  }
  const seconds = Math.floor(delta / 1_000);
  if (seconds < 60) {
    return `${seconds} 秒前`;
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${minutes} 分钟前`;
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return `${hours} 小时前`;
  }
  return `${Math.floor(hours / 24)} 天前`;
}

export function formatCooldown(cooldownMs: number): string {
  if (cooldownMs <= 0) {
    return "无冷却";
  }
  if (cooldownMs % 60_000 === 0) {
    return `${cooldownMs / 60_000} 分钟`;
  }
  if (cooldownMs % 1_000 === 0) {
    return `${cooldownMs / 1_000} 秒`;
  }
  return `${cooldownMs} 毫秒`;
}

const DIRECTION_SYMBOL: Record<AlertDirection, string> = {
  ABOVE: "≥",
  BELOW: "≤",
};

const CROSS_LABEL: Record<AlertDirection, string> = {
  ABOVE: "上穿",
  BELOW: "下穿",
};

export function indicatorLabel(
  name: IndicatorName,
  period: number | null | undefined,
  channel: string | null | undefined,
): string {
  const label = INDICATOR_LABELS[name] ?? name;
  const suffix = channel !== null && channel !== undefined && channel !== "value" ? ` ${channel}` : "";
  return `${label}${period ? `(${period})` : ""}${suffix}`;
}

/** 条件摘要只消费后端契约字段；画线类型由调用方按 id 补进来。 */
export function conditionSummary(
  condition: AlertCondition,
  drawingKind?: DrawingKind | null,
): string {
  const direction = DIRECTION_SYMBOL[condition.direction];
  switch (condition.kind) {
    case "METRIC_THRESHOLD":
      return `${METRIC_LABELS[condition.metric ?? "last_price"]} ${direction} ${condition.threshold ?? "?"}`;
    case "INDICATOR_SIGNAL": {
      const left = indicatorLabel(
        condition.indicator ?? "EMA",
        condition.indicator_period,
        condition.indicator_channel,
      );
      if (condition.compare_indicator !== null && condition.compare_indicator !== undefined) {
        const right = indicatorLabel(
          condition.compare_indicator,
          condition.compare_period,
          null,
        );
        return `${left} ${CROSS_LABEL[condition.direction]} ${right}`;
      }
      return `${left} ${direction} ${condition.threshold ?? "?"}`;
    }
    case "OVERLAY_CROSS": {
      const kind = drawingKind ? KIND_LABELS[drawingKind] : "画线";
      const mode = condition.overlay_mode === "TOUCH" ? "盘中触碰" : "收盘突破";
      return `${kind} ${mode}（${CROSS_LABEL[condition.direction]}）`;
    }
  }
}

export function triggerSummary(trigger: AlertTriggerRecord): string {
  if (trigger.message.trim().length > 0) {
    return trigger.message;
  }
  return `${trigger.instrument_id} ${trigger.timeframe} 条件触发`;
}

export interface AlertFormState {
  instrumentId: string;
  timeframe: string;
  kind: AlertConditionKind;
  metric: AlertMetric;
  threshold: string;
  direction: AlertDirection;
  indicator: IndicatorName;
  indicatorPeriod: number;
  indicatorChannel: string;
  compareIndicator: IndicatorName | "";
  comparePeriod: number;
  drawingId: string;
  overlayMode: AlertOverlayMode;
  note: string;
  cooldownSeconds: number;
  repeat: boolean;
  draft: boolean;
}

export function defaultAlertForm(
  instrumentId: string,
  timeframe: string,
  options: { drawingId?: string } = {},
): AlertFormState {
  return {
    instrumentId,
    timeframe,
    kind: options.drawingId ? "OVERLAY_CROSS" : "METRIC_THRESHOLD",
    metric: "last_price",
    threshold: "",
    direction: "ABOVE",
    indicator: "RSI",
    indicatorPeriod: 14,
    indicatorChannel: "value",
    compareIndicator: "",
    comparePeriod: 0,
    drawingId: options.drawingId ?? "",
    overlayMode: "CLOSE_BREAK",
    note: "",
    cooldownSeconds: 60,
    repeat: true,
    draft: false,
  };
}

export function validateAlertForm(form: AlertFormState): string | null {
  if (!form.instrumentId.trim()) {
    return "请选择标的";
  }
  if (!form.timeframe.trim()) {
    return "请选择周期";
  }
  if (form.kind === "METRIC_THRESHOLD") {
    if (!isDecimalString(form.threshold)) {
      return "请输入有效的阈值";
    }
  }
  if (form.kind === "INDICATOR_SIGNAL") {
    if (!form.compareIndicator && !isDecimalString(form.threshold)) {
      return "指标信号需要阈值或对比指标";
    }
    if (form.compareIndicator && form.indicator === form.compareIndicator) {
      return "对比指标不能与主指标相同";
    }
    if (form.indicatorPeriod < 1 || form.indicatorPeriod > 5_000) {
      return "指标周期需在 1 到 5000 之间";
    }
  }
  if (form.kind === "OVERLAY_CROSS" && !form.drawingId) {
    return "请选择要挂载的画线";
  }
  if (form.cooldownSeconds < 0) {
    return "冷却时间不能为负";
  }
  if (form.note.length > 500) {
    return "备注最多 500 字";
  }
  return null;
}

function isDecimalString(value: string): boolean {
  return /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value.trim());
}

export function buildAlertCondition(form: AlertFormState): AlertCondition {
  if (form.kind === "METRIC_THRESHOLD") {
    return {
      kind: "METRIC_THRESHOLD",
      direction: form.direction,
      metric: form.metric,
      threshold: form.threshold.trim() as DecimalString,
    };
  }
  if (form.kind === "INDICATOR_SIGNAL") {
    const useCompare = form.compareIndicator !== "";
    return {
      kind: "INDICATOR_SIGNAL",
      direction: form.direction,
      indicator: form.indicator,
      indicator_period: form.indicatorPeriod,
      indicator_channel: form.indicatorChannel,
      ...(useCompare
        ? {
            compare_indicator: form.compareIndicator as IndicatorName,
            compare_period: form.comparePeriod,
          }
        : { threshold: form.threshold.trim() as DecimalString }),
    };
  }
  return {
    kind: "OVERLAY_CROSS",
    direction: form.direction,
    drawing_id: form.drawingId,
    overlay_mode: form.overlayMode,
  };
}

export function buildAlertCreate(form: AlertFormState): AlertCreate {
  return {
    instrument_id: form.instrumentId,
    timeframe: form.timeframe,
    condition: buildAlertCondition(form),
    note: form.note,
    draft: form.draft,
    repeat: form.repeat,
    cooldown_ms: form.repeat ? Math.round(form.cooldownSeconds * 1_000) : 0,
  };
}

export function formFromRecord(record: AlertRecord): AlertFormState {
  const condition = record.condition;
  const base = defaultAlertForm(record.instrument_id, record.timeframe, {
    drawingId: condition.drawing_id ?? undefined,
  });
  return {
    ...base,
    kind: condition.kind,
    metric: condition.metric ?? base.metric,
    threshold: condition.threshold ?? "",
    direction: condition.direction,
    indicator: (condition.indicator ?? base.indicator) as IndicatorName,
    indicatorPeriod: condition.indicator_period ?? base.indicatorPeriod,
    indicatorChannel: condition.indicator_channel ?? base.indicatorChannel,
    compareIndicator:
      (condition.compare_indicator as IndicatorName | null | undefined) ?? "",
    comparePeriod: condition.compare_period ?? 0,
    drawingId: condition.drawing_id ?? "",
    overlayMode: condition.overlay_mode ?? base.overlayMode,
    note: record.note ?? "",
    cooldownSeconds: Math.round(record.cooldown_ms / 1_000),
    repeat: record.repeat,
    draft: record.status === "DRAFT",
  };
}

export function drawingKindFor(
  drawingId: string,
  drawings: { drawing_id: string; kind: DrawingKind }[],
): DrawingKind | null {
  return drawings.find((drawing) => drawing.drawing_id === drawingId)?.kind ?? null;
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim()) {
    return error.message;
  }
  return "预警操作失败，请稍后重试";
}
