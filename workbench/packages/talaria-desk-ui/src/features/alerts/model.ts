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
} from "@talaria/contracts";

import { t } from "../../lib/i18n";
import { drawingKindLabel } from "../drawings/tools";
import { indicatorLabel as localizedIndicatorLabel } from "../indicators/config";

export { drawingKindLabel };

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
    case "COOLDOWN": return t("冷却中", "Cooling");
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
    case "mark_price": return t("标记价", "Mark price");
    case "funding_rate": return t("资金费率", "Funding rate");
    case "open_interest": return t("持仓量", "Open interest");
  }
}

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
    case "TOUCH": return t("触及关键位", "Touches level");
    case "BREAK": return t("收盘突破", "Closes beyond level");
    case "RETEST": return t("回踩确认", "Retests level");
  }
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
    case "ARMED": return "active";
    case "COOLDOWN": return "cooling";
    case "DRAFT": return "draft";
    case "EXPIRED":
    case "DISABLED": return "expired";
  }
}

export function alertsForTab(alerts: AlertRecord[], tab: AlertTabId): AlertRecord[] {
  return alerts
    .filter((a) => statusToTab(a.status) === tab)
    .slice()
    .sort((l, r) => r.updated_ts - l.updated_ts || l.alert_id.localeCompare(r.alert_id));
}

export function tabCounts(alerts: AlertRecord[]): Record<AlertTabId, number> {
  const counts: Record<AlertTabId, number> = {
    active: 0,
    cooling: 0,
    expired: 0,
    draft: 0,
  };
  for (const alert of alerts) counts[statusToTab(alert.status)] += 1;
  return counts;
}

export function relativeTime(ts: number, now: number): string {
  const delta = Math.max(0, now - ts);
  if (delta < 5_000) return t("刚刚", "just now");
  const s = Math.floor(delta / 1_000);
  if (s < 60) return t(`${s} 秒前`, `${s}s ago`);
  const m = Math.floor(s / 60);
  if (m < 60) return t(`${m} 分前`, `${m}m ago`);
  const h = Math.floor(m / 60);
  return h < 24 ? t(`${h} 时前`, `${h}h ago`) : t(`${Math.floor(h / 24)} 天前`, `${Math.floor(h / 24)}d ago`);
}

export function formatCooldown(cooldownMs: number): string {
  if (cooldownMs <= 0) return t("无冷却", "No cooldown");
  if (cooldownMs % 60_000 === 0) return `${cooldownMs / 60_000}m`;
  return `${Math.round(cooldownMs / 1000)}s`;
}

export function conditionSummary(rule: Rule, drawingKind?: DrawingKind | null): string {
  const node = rule.expression;
  switch (node.op) {
    case "compare": {
      const left =
        node.left.source === "event"
          ? metricLabel(node.left.metric)
          : node.left.source === "indicator"
            ? localizedIndicatorLabel(
                node.left.name === "legacy_ema"
                  ? "EMA"
                  : node.left.name === "legacy_atr"
                    ? "ATR"
                    : node.left.name,
              )
            : node.left.source;
      const right =
        node.right.source === "constant" ? node.right.value : node.right.source;
      return `${left} ${node.test} ${right}`;
    }
    case "overlay": {
      const kind = drawingKind ? drawingKindLabel(drawingKind) : t("画线", "Drawing");
      return `${kind} ${node.mode} (${node.direction})`;
    }
    case "level_event":
      return `${t("关键位", "Level")} ${node.level_id} · ${alertLevelEventLabel(node.event)}`;
    default:
      return t("组合规则", "Composite rule");
  }
}

export function triggerSummary(trigger: AlertTriggerRecord): string {
  if (trigger.message.trim().length > 0) return trigger.message;
  return `${trigger.instrument_id} ${trigger.timeframe}`;
}

export type AlertFormKind = "metric" | "indicator" | "overlay" | "level";

export interface AlertFormState {
  instrumentId: string;
  timeframe: string;
  kind: AlertFormKind;
  metric: AlertMetric;
  threshold: string;
  direction: AlertDirection;
  indicator: IndicatorName;
  indicatorPeriod: number;
  drawingId: string;
  overlayMode: AlertOverlayMode;
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
    price?: number | null;
    levelId?: string;
    levelKind?: KeyLevelKind | null;
  } = {},
): AlertFormState {
  const kind: AlertFormKind = options.levelId
    ? "level"
    : options.drawingId
      ? "overlay"
      : "metric";
  return {
    instrumentId,
    timeframe,
    kind,
    metric: "last_price",
    threshold:
      options.price !== null && options.price !== undefined && Number.isFinite(options.price)
        ? String(options.price)
        : "",
    direction: "ABOVE",
    indicator: "RSI",
    indicatorPeriod: 14,
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

function isDecimalString(value: string): boolean {
  return /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value.trim());
}

export function validateAlertForm(form: AlertFormState): string | null {
  if (!form.instrumentId.trim()) return t("请选择标的", "Select an instrument");
  if (!form.timeframe.trim()) return t("请选择周期", "Select a timeframe");
  if (form.kind === "metric" && !isDecimalString(form.threshold)) {
    return t("请输入有效价格阈值", "Enter a valid decimal threshold");
  }
  if (form.kind === "indicator" && !isDecimalString(form.threshold)) {
    return t("请输入指标阈值", "Enter an indicator threshold");
  }
  if (form.kind === "overlay" && !form.drawingId) {
    return t("请选择要绑定的画线", "Select a drawing");
  }
  if (form.kind === "level" && !form.levelId.trim()) {
    return t("请选择关键位", "Select a key level");
  }
  return null;
}

export function buildAlertRule(form: AlertFormState): Rule {
  const test = form.direction === "ABOVE" ? "gte" : "lte";
  const constant = { source: "constant", value: form.threshold.trim() as DecimalString } as const;
  if (form.kind === "metric") {
    return {
      version: "1",
      timing: "event",
      expression: {
        op: "compare",
        test,
        left: { source: "event", metric: form.metric },
        right: constant,
      },
    };
  }
  if (form.kind === "indicator") {
    return {
      version: "1",
      timing: "bar_close",
      expression: {
        op: "compare",
        test,
        left: {
          source: "indicator",
          name: form.indicator,
          params: { period: form.indicatorPeriod },
        },
        right: constant,
      },
    };
  }
  if (form.kind === "level") {
    return {
      version: "1",
      timing: form.levelEvent === "TOUCH" ? "event" : "bar_close",
      expression: {
        op: "level_event",
        level_id: form.levelId.trim(),
        event: form.levelEvent,
        ...(form.levelKind !== null ? { level_kind: form.levelKind } : {}),
      },
    };
  }
  return {
    version: "1",
    timing: form.overlayMode === "TOUCH" ? "event" : "bar_close",
    expression: {
      op: "overlay",
      direction: form.direction,
      drawing_id: form.drawingId,
      mode: form.overlayMode,
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

export function drawingKindFor(
  drawingId: string,
  drawings: readonly { drawing_id: string; kind: DrawingKind }[],
): DrawingKind | null {
  return drawings.find((d) => d.drawing_id === drawingId)?.kind ?? null;
}
