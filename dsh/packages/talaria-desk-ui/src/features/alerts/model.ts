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

import { t } from "../../lib/i18n";
import { drawingKindLabel } from "../drawings/tools";
import { indicatorLabel as localizedIndicatorLabel } from "../indicators/config";

export { drawingKindLabel };

/** 规则列表的四个页签；DISABLED 与 EXPIRED 都归入“已结束”。 */
export const ALERT_TABS = ["active", "cooling", "expired", "draft"] as const;

export type AlertTabId = (typeof ALERT_TABS)[number];

export function alertTabLabel(tab: AlertTabId): string {
  switch (tab) {
    case "active": return t("活跃", "Active");
    case "cooling": return t("冷却", "Cooling");
    case "expired": return t("已过期", "Expired");
    case "draft": return t("草案", "Drafts");
  }
}

export function alertStatusLabel(status: AlertStatus): string {
  switch (status) {
    case "ARMED": return t("待命", "Armed");
    case "COOLDOWN": return t("冷却中", "Cooling down");
    case "EXPIRED": return t("已过期", "Expired");
    case "DISABLED": return t("已停用", "Disabled");
    case "DRAFT": return t("草案", "Draft");
  }
}

export function metricLabel(metric: AlertMetric): string {
  switch (metric) {
    case "last_price": return t("最新价", "Last price");
    case "bid_price": return t("买一价", "Bid price");
    case "ask_price": return t("卖一价", "Ask price");
    case "mark_price": return t("标记价格", "Mark price");
    case "funding_rate": return t("资金费率", "Funding rate");
    case "open_interest": return t("持仓量", "Open interest");
  }
}

/** Built at render time so a runtime locale switch refreshes every option. */
export function metricLabels(): Record<AlertMetric, string> {
  return {
    last_price: metricLabel("last_price"),
    bid_price: metricLabel("bid_price"),
    ask_price: metricLabel("ask_price"),
    mark_price: metricLabel("mark_price"),
    funding_rate: metricLabel("funding_rate"),
    open_interest: metricLabel("open_interest"),
  };
}

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

export function alertLevelEventLabel(event: AlertLevelEvent): string {
  switch (event) {
    case "TOUCH": return t("触及关键位", "Touches key level");
    case "BREAK": return t("收盘突破关键位", "Closes beyond key level");
    case "RETEST": return t("突破后回踩确认", "Retests after breakout");
  }
}

export function alertLevelEventLabels(): Record<AlertLevelEvent, string> {
  return {
    TOUCH: alertLevelEventLabel("TOUCH"),
    BREAK: alertLevelEventLabel("BREAK"),
    RETEST: alertLevelEventLabel("RETEST"),
  };
}

export function indicatorNameLabel(name: RuleIndicator["name"]): string {
  return name === "legacy_ema" || name === "legacy_atr" ? name : localizedIndicatorLabel(name);
}

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
    return t("刚刚", "just now");
  }
  const seconds = Math.floor(delta / 1_000);
  if (seconds < 60) {
    return t(`${seconds} 秒前`, `${seconds}s ago`);
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return t(`${minutes} 分钟前`, `${minutes}m ago`);
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return t(`${hours} 小时前`, `${hours}h ago`);
  }
  return t(`${Math.floor(hours / 24)} 天前`, `${Math.floor(hours / 24)}d ago`);
}

/** 未来时刻（例如冷却结束回到待命）。 */
export function timeUntil(ts: number, now: number): string {
  const delta = Math.max(0, ts - now);
  if (delta < 5_000) {
    return t("即将", "very soon");
  }
  const seconds = Math.floor(delta / 1_000);
  if (seconds < 60) {
    return t(`${seconds} 秒后`, `in ${seconds}s`);
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return t(`${minutes} 分钟后`, `in ${minutes}m`);
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return t(`${hours} 小时后`, `in ${hours}h`);
  }
  return t(`${Math.floor(hours / 24)} 天后`, `in ${Math.floor(hours / 24)}d`);
}

export function formatCooldown(cooldownMs: number): string {
  if (cooldownMs <= 0) {
    return t("无冷却", "No cooldown");
  }
  if (cooldownMs % 60_000 === 0) {
    return t(`${cooldownMs / 60_000} 分钟`, `${cooldownMs / 60_000} min`);
  }
  if (cooldownMs % 1_000 === 0) {
    return t(`${cooldownMs / 1_000} 秒`, `${cooldownMs / 1_000} s`);
  }
  return t(`${cooldownMs} 毫秒`, `${cooldownMs} ms`);
}

const COMPARISON_SYMBOLS: Record<RuleComparison["test"], string> = {
  lt: "<",
  lte: "≤",
  gt: ">",
  gte: "≥",
  eq: "=",
  cross_above: "cross_above",
  cross_below: "cross_below",
};

function crossLabel(direction: AlertDirection): string {
  return direction === "ABOVE" ? t("上穿", "crosses above") : t("下穿", "crosses below");
}

function comparisonLabel(test: RuleComparison["test"]): string {
  if (test === "cross_above") return t("上穿", "crosses above");
  if (test === "cross_below") return t("下穿", "crosses below");
  return COMPARISON_SYMBOLS[test];
}

export function indicatorLabel(
  name: IndicatorName,
  period: number | null | undefined,
  channel: string | null | undefined,
): string {
  const label = indicatorNameLabel(name);
  const suffix = channel !== null && channel !== undefined && channel !== "value" ? ` ${channel}` : "";
  return `${label}${period ? `(${period})` : ""}${suffix}`;
}

function indicatorName(operator: RuleIndicator["name"]): IndicatorName {
  if (operator === "legacy_ema") return "EMA";
  if (operator === "legacy_atr") return "ATR";
  return operator;
}

export function barFieldLabel(field: keyof typeof BAR_FIELD_KEYS): string {
  switch (field) {
    case "open": return t("开盘价", "Open");
    case "high": return t("最高价", "High");
    case "low": return t("最低价", "Low");
    case "close": return t("收盘价", "Close");
    case "volume": return t("成交量", "Volume");
    case "turnover": return t("成交额", "Turnover");
  }
}

const BAR_FIELD_KEYS = {
  open: true, high: true, low: true, close: true,
  volume: true, turnover: true,
};

function operandSummary(operand: RuleComparison["left"]): string {
  switch (operand.source) {
    case "constant": return operand.value;
    case "drawing_price":
      return t(
        `画线 ${operand.drawing_id} ${operand.edge === "lower" ? "下沿" : "上沿"}`,
        `Drawing ${operand.drawing_id} ${operand.edge === "lower" ? "lower edge" : "upper edge"}`,
      );
    case "level_price":
      return t(
        `关键位 ${operand.level_id} ${operand.edge === "lower" ? "下沿" : "上沿"}`,
        `Key level ${operand.level_id} ${operand.edge === "lower" ? "lower edge" : "upper edge"}`,
      );
    case "bar": return barFieldLabel(operand.field);
    case "event": return metricLabel(operand.metric);
    case "derivative": return metricLabel(operand.kind);
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
          ? crossLabel(node.test === "gte" ? "ABOVE" : "BELOW")
          : comparisonLabel(node.test);
        return `${operandSummary(node.left)} ${operator} ${operandSummary(node.right)}`;
      }
      case "overlay": {
        const kind = drawingKind ? drawingKindLabel(drawingKind) : t("画线", "Drawing");
        const mode = node.mode === "TOUCH"
          ? (rule.timing === "event"
            ? t("盘中触碰", "intraday touch")
            : t("收盘确认触碰", "close-confirmed touch"))
          : t("收盘突破", "close break");
        return t(
          `${kind} ${mode}（${crossLabel(node.direction)}）`,
          `${kind} ${mode} (${crossLabel(node.direction)})`,
        );
      }
      case "level_event":
        return `${t("关键位", "Key level")} ${node.level_id} · ${alertLevelEventLabel(node.event)}`;
      case "not":
        return t(`非（${summarize(node.args[0])}）`, `not (${summarize(node.args[0])})`);
      case "all":
      case "any":
        return t(
          `（${node.args.map(summarize).join(node.op === "all" ? " 且 " : " 或 ")}）`,
          `(${node.args.map(summarize).join(node.op === "all" ? " and " : " or ")})`,
        );
    }
  }
  return summarize(rule.expression);
}

export function triggerSummary(trigger: AlertTriggerRecord): string {
  if (trigger.message.trim().length > 0) {
    return trigger.message;
  }
  return t(
    `${trigger.instrument_id} ${trigger.timeframe} 条件触发`,
    `${trigger.instrument_id} ${trigger.timeframe} condition triggered`,
  );
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
    return t("请选择标的", "Select an instrument");
  }
  if (!form.timeframe.trim()) {
    return t("请选择周期", "Select a timeframe");
  }
  if (form.kind === "metric") {
    if (!isDecimalString(form.threshold)) {
      return t("请输入有效的阈值", "Enter a valid threshold");
    }
  }
  if (form.kind === "indicator") {
    if (!form.compareIndicator && !isDecimalString(form.threshold)) {
      return t(
        "指标信号需要阈值或对比指标",
        "Indicator signals need a threshold or a comparison indicator",
      );
    }
    if (form.compareIndicator && form.indicator === form.compareIndicator) {
      return t(
        "对比指标不能与主指标相同",
        "The comparison indicator must differ from the main indicator",
      );
    }
    if (
      form.indicatorPeriod !== 0 &&
      (form.indicatorPeriod < 1 || form.indicatorPeriod > 5_000)
    ) {
      return t(
        "指标周期需在 1 到 5000 之间（0 表示服务端默认）",
        "Indicator period must be between 1 and 5000 (0 uses the server default)",
      );
    }
    if (
      form.compareIndicator &&
      form.comparePeriod !== 0 &&
      (form.comparePeriod < 1 || form.comparePeriod > 5_000)
    ) {
      return t(
        "对比周期需在 1 到 5000 之间（0 表示服务端默认）",
        "Comparison period must be between 1 and 5000 (0 uses the server default)",
      );
    }
  }
  if (form.kind === "overlay" && !form.drawingId) {
    return t("请选择要挂载的画线", "Select a drawing to attach");
  }
  if (form.kind === "level" && !form.levelId.trim()) {
    return t(
      "请从关键位面板选择或填写 level_id",
      "Choose a key level or enter a level_id",
    );
  }
  if (form.cooldownSeconds < 0) {
    return t("冷却时间不能为负", "Cooldown cannot be negative");
  }
  if (form.note.length > 500) {
    return t("备注最多 500 字", "Note must be 500 characters or fewer");
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
  return t("预警操作失败，请稍后重试", "Alert action failed. Please try again.");
}
