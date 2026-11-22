import type {
  AlertCreate,
  AlertDirection,
  AlertLevelEvent,
  AlertMetric,
  AlertOverlayMode,
  AlertRecord,
  AlertStatus,
  AlertTriggerRecord,
  DecimalString,
  DrawingKind,
  IndicatorName,
  KeyLevelKind,
  Rule,
  RuleComparison,
  RuleIndicator,
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

/** 快捷预警表单提供常见的技术指标。 */
export const ALERT_INDICATORS: IndicatorName[] = [
  "EMA",
  "SMA",
  "BOLL",
  "VWAP",
  "RSI",
  "ATR",
];

export const ALERT_LEVEL_EVENTS: AlertLevelEvent[] = ["TOUCH", "BREAK", "RETEST"];

export const ALERT_LEVEL_EVENT_LABELS: Record<AlertLevelEvent, string> = {
  TOUCH: "触及关键位",
  BREAK: "收盘突破关键位",
  RETEST: "突破后回踩确认",
};

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

const COMPARISON_LABELS: Record<RuleComparison["test"], string> = {
  lt: "<",
  lte: "≤",
  gt: ">",
  gte: "≥",
  eq: "=",
  cross_above: "上穿",
  cross_below: "下穿",
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

function indicatorName(operator: RuleIndicator["name"]): IndicatorName {
  if (operator === "legacy_ema") return "EMA";
  if (operator === "legacy_atr") return "ATR";
  return operator;
}

const BAR_FIELDS = {
  open: "开盘价", high: "最高价", low: "最低价", close: "收盘价",
  volume: "成交量", turnover: "成交额",
};

function operandSummary(operand: RuleComparison["left"]): string {
  switch (operand.source) {
    case "constant": return operand.value;
    case "drawing_price": return `画线 ${operand.drawing_id} ${operand.edge === "lower" ? "下沿" : "上沿"}`;
    case "level_price": return `关键位 ${operand.level_id} ${operand.edge === "lower" ? "下沿" : "上沿"}`;
    case "bar": return BAR_FIELDS[operand.field];
    case "event": return METRIC_LABELS[operand.metric];
    case "derivative": return METRIC_LABELS[operand.kind];
    case "indicator":
      return indicatorLabel(indicatorName(operand.name), operand.params?.period, operand.channel);
  }
}

export function conditionSummary(rule: Rule, drawingKind?: DrawingKind | null): string {
  function summarize(node: Rule["expression"]): string {
    switch (node.op) {
      case "compare": {
        const operator = node.left.source === "indicator" && node.right.source === "indicator"
          && (node.test === "gte" || node.test === "lte")
          ? CROSS_LABEL[node.test === "gte" ? "ABOVE" : "BELOW"]
          : COMPARISON_LABELS[node.test];
        return `${operandSummary(node.left)} ${operator} ${operandSummary(node.right)}`;
      }
      case "overlay": {
        const kind = drawingKind ? KIND_LABELS[drawingKind] : "画线";
        const mode = node.mode === "TOUCH"
          ? (rule.timing === "event" ? "盘中触碰" : "收盘确认触碰")
          : "收盘突破";
        return `${kind} ${mode}（${CROSS_LABEL[node.direction]}）`;
      }
      case "level_event":
        return `关键位 ${node.level_id} · ${ALERT_LEVEL_EVENT_LABELS[node.event]}`;
      case "not":
        return `非（${summarize(node.args[0])}）`;
      case "all":
      case "any":
        return `（${node.args.map(summarize).join(node.op === "all" ? " 且 " : " 或 ")}）`;
    }
  }
  return summarize(rule.expression);
}

export function triggerSummary(trigger: AlertTriggerRecord): string {
  if (trigger.message.trim().length > 0) {
    return trigger.message;
  }
  return `${trigger.instrument_id} ${trigger.timeframe} 条件触发`;
}

export type AlertFormKind = "metric" | "indicator" | "overlay" | "level" | "rule";

export interface AlertFormState {
  instrumentId: string;
  timeframe: string;
  kind: AlertFormKind;
  savedRule?: Rule;
  savedIndicator?: RuleIndicator;
  savedCompare?: RuleIndicator;
  metric: AlertMetric;
  threshold: string;
  direction: AlertDirection;
  indicator: IndicatorName;
  indicatorPeriod: number;
  indicatorAnchor: "" | "snapshot" | "session";
  indicatorChannel: string;
  compareIndicator: IndicatorName | "";
  comparePeriod: number;
  compareAnchor: "" | "snapshot" | "session";
  drawingId: string;
  overlayMode: AlertOverlayMode;
  overlayRatio?: DecimalString | null;
  levelId: string;
  levelEvent: AlertLevelEvent;
  levelKind: KeyLevelKind | null;
  note: string;
  cooldownSeconds: number;
  repeat: boolean;
  draft: boolean;
}

export function defaultAlertForm(
  instrumentId: string,
  timeframe: string,
  options: {
    drawingId?: string;
    levelId?: string;
    levelKind?: KeyLevelKind | null;
  } = {},
): AlertFormState {
  return {
    instrumentId,
    timeframe,
    kind: options.levelId
      ? "level"
      : options.drawingId
        ? "overlay"
        : "metric",
    metric: "last_price",
    threshold: "",
    direction: "ABOVE",
    indicator: "RSI",
    indicatorPeriod: 0,
    indicatorAnchor: "",
    indicatorChannel: "value",
    compareIndicator: "",
    comparePeriod: 0,
    compareAnchor: "",
    drawingId: options.drawingId ?? "",
    overlayMode: "CLOSE_BREAK",
    levelId: options.levelId ?? "",
    levelEvent: "TOUCH",
    levelKind: options.levelKind ?? null,
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
  if (form.kind === "metric") {
    if (!isDecimalString(form.threshold)) {
      return "请输入有效的阈值";
    }
  }
  if (form.kind === "indicator") {
    if (!form.compareIndicator && !isDecimalString(form.threshold)) {
      return "指标信号需要阈值或对比指标";
    }
    if (form.compareIndicator && form.indicator === form.compareIndicator) {
      return "对比指标不能与主指标相同";
    }
    if (
      form.indicatorPeriod !== 0 &&
      (form.indicatorPeriod < 1 || form.indicatorPeriod > 5_000)
    ) {
      return "指标周期需在 1 到 5000 之间（0 表示服务端默认）";
    }
    if (
      form.compareIndicator &&
      form.comparePeriod !== 0 &&
      (form.comparePeriod < 1 || form.comparePeriod > 5_000)
    ) {
      return "对比周期需在 1 到 5000 之间（0 表示服务端默认）";
    }
  }
  if (form.kind === "overlay" && !form.drawingId) {
    return "请选择要挂载的画线";
  }
  if (form.kind === "level" && !form.levelId.trim()) {
    return "请从关键位面板选择或填写 level_id";
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

export function buildAlertRule(form: AlertFormState): Rule {
  if (form.savedRule) return form.savedRule;
  const test = form.direction === "ABOVE" ? "gte" : "lte";
  const constant = { source: "constant", value: form.threshold.trim() } as const;
  if (form.kind === "metric") {
    return { version: "1", timing: "event", expression: {
      op: "compare", test,
      left: { source: "event", metric: form.metric },
      right: constant,
    } };
  }
  if (form.kind === "indicator") {
    const indicator = (
      name: IndicatorName, period: number, anchor: AlertFormState["indicatorAnchor"],
      channel: string | null | undefined, saved: RuleIndicator | undefined,
    ): RuleIndicator => {
      const same = saved && indicatorName(saved.name) === name;
      const params = {
        ...(period > 0 ? { period } : {}),
        ...(name === "VWAP" && anchor ? { anchor } : {}),
        ...(same && saved.params?.std_dev != null ? { std_dev: saved.params.std_dev } : {}),
      };
      return {
        source: "indicator",
        name: same ? saved.name : name,
        ...(channel != null ? { channel } : {}),
        ...(Object.keys(params).length ? { params } : {}),
      };
    };
    return { version: "1", timing: "bar_close", expression: {
      op: "compare", test,
      left: indicator(form.indicator, form.indicatorPeriod, form.indicatorAnchor,
        form.indicatorChannel, form.savedIndicator),
      right: form.compareIndicator
        ? indicator(form.compareIndicator, form.comparePeriod, form.compareAnchor,
          form.savedCompare?.channel, form.savedCompare)
        : constant,
    } };
  }
  if (form.kind === "level") {
    return {
      version: "1", timing: form.levelEvent === "TOUCH" ? "event" : "bar_close",
      expression: {
        op: "level_event", level_id: form.levelId.trim(), event: form.levelEvent,
        ...(form.levelKind !== null ? { level_kind: form.levelKind } : {}),
      },
    };
  }
  return {
    version: "1", timing: form.overlayMode === "TOUCH" ? "event" : "bar_close",
    expression: {
      op: "overlay", direction: form.direction, drawing_id: form.drawingId,
      mode: form.overlayMode,
      ...(form.overlayRatio != null ? { ratio: form.overlayRatio } : {}),
    },
  };
}

export function buildAlertCreate(form: AlertFormState): AlertCreate {
  return {
    instrument_id: form.instrumentId,
    timeframe: form.timeframe,
    condition: buildAlertRule(form),
    note: form.note,
    draft: form.draft,
    repeat: form.repeat,
    cooldown_ms: form.repeat ? Math.round(form.cooldownSeconds * 1_000) : 0,
  };
}

export function formFromRecord(record: AlertRecord): AlertFormState {
  const rule = record.condition;
  const node = rule.expression;
  const base = {
    ...defaultAlertForm(record.instrument_id, record.timeframe),
    note: record.note ?? "",
    cooldownSeconds: Math.round(record.cooldown_ms / 1_000),
    repeat: record.repeat,
    draft: record.status === "DRAFT",
  };
  if (node.op === "overlay" && (rule.timing === "event") === (node.mode === "TOUCH")) {
    return { ...base, kind: "overlay", drawingId: node.drawing_id, direction: node.direction,
      overlayMode: node.mode, overlayRatio: node.ratio };
  }
  if (node.op === "level_event" && (rule.timing === "event") === (node.event === "TOUCH")) {
    return { ...base, kind: "level", levelId: node.level_id, levelEvent: node.event,
      levelKind: node.level_kind ?? null };
  }
  if (node.op === "compare" && (node.test === "gte" || node.test === "lte")) {
    const direction = node.test === "gte" ? "ABOVE" : "BELOW";
    const left = node.left;
    const right = node.right;
    if (left.source === "event" && right.source === "constant") {
      return { ...base, kind: "metric", metric: left.metric, direction, threshold: right.value };
    }
    if (left.source === "indicator" && ALERT_INDICATORS.includes(indicatorName(left.name))
      && (right.source === "constant" || (right.source === "indicator"
        && ALERT_INDICATORS.includes(indicatorName(right.name))
        && indicatorName(left.name) !== indicatorName(right.name)))) {
      const name = indicatorName(left.name);
      return {
        ...base, kind: "indicator", direction, indicator: name, savedIndicator: left,
        indicatorPeriod: left.params?.period ?? 0,
        indicatorAnchor: left.params?.anchor ?? "",
        indicatorChannel: left.channel ?? (name === "BOLL" ? "mid" : "value"),
        threshold: right.source === "constant" ? right.value : "",
        compareIndicator: right.source === "indicator" ? indicatorName(right.name) : "",
        comparePeriod: right.source === "indicator" ? right.params?.period ?? 0 : 0,
        compareAnchor: right.source === "indicator" ? right.params?.anchor ?? "" : "",
        savedCompare: right.source === "indicator" ? right : undefined,
      };
    }
  }
  return { ...base, kind: "rule", savedRule: rule };
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
