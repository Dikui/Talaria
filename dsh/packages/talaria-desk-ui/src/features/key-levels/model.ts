import type {
  EvidenceClass,
  KeyLevel,
  KeyLevelCategory,
  KeyLevelKind,
  KeyLevelStatus,
} from "@talaria/contracts";

import { t } from "../../lib/i18n";

/**
 * 关键位展示模型：过滤、价格格式化与标签避让。
 *
 * 这里全部是纯函数，像素坐标由调用方从 KLineChart 投影后传入，
 * 因此可以直接对“确认延迟可见性、贴边翻转、省略、避让”做确定性单测。
 */

export const KEY_LEVEL_CATEGORIES: readonly KeyLevelCategory[] = [
  "STRUCTURE",
  "SESSION",
  "VOLUME",
  "GAP",
  "ROUND_NUMBER",
  "LIQUIDATION",
];

export interface CategoryMeta {
  label: string;
  color: string;
  description: string;
}

/** Built at render time so a runtime locale switch refreshes the panel. */
export function categoryMeta(category: KeyLevelCategory): CategoryMeta {
  switch (category) {
    case "STRUCTURE":
      return {
        label: t("结构位", "Structure"),
        color: "#4c8dff",
        description: t("摆动高低点与多次触及区间", "Swing highs/lows and repeatedly tested ranges"),
      };
    case "SESSION":
      return {
        label: t("时段位", "Session"),
        color: "#f2b84b",
        description: t(
          "前日高低收、周/月开盘与时段高低",
          "Previous day high/low/close, weekly/monthly opens and session highs/lows",
        ),
      };
    case "GAP":
      return {
        label: t("缺口", "Gap"),
        color: "#b07cff",
        description: t("未回补跳空缺口", "Unfilled price gaps"),
      };
    case "ROUND_NUMBER":
      return {
        label: t("整数关口", "Round number"),
        color: "#48c9a9",
        description: t("按价格量级取整的关口", "Levels rounded by price magnitude"),
      };
    case "VOLUME":
      return {
        label: t("成交量位", "Volume"),
        color: "#e58f65",
        description: t("POC/VAH/VAL 与锚定 VWAP ±σ 带", "POC/VAH/VAL and anchored VWAP ±σ bands"),
      };
    case "LIQUIDATION":
      return {
        label: t("清算估算", "Liquidation estimate"),
        color: "#ff6b7a",
        description: t(
          "由持仓量变化与杠杆假设推算，非观测成交",
          "Derived from open-interest changes and leverage assumptions, not observed trades",
        ),
      };
  }
}

export const EVIDENCE_CLASSES: readonly EvidenceClass[] = [
  "OBSERVED",
  "ESTIMATED",
];

export function evidenceMeta(evidence: EvidenceClass): { label: string; description: string } {
  return evidence === "OBSERVED"
    ? {
        label: t("观测", "Observed"),
        description: t("由已闭合的成交与报价直接计算", "Computed directly from closed trades and quotes"),
      }
    : {
        label: t("估算", "Estimated"),
        description: t(
          "由持仓量变化与杠杆假设推算，仅作参考",
          "Derived from open-interest changes and leverage assumptions; reference only",
        ),
      };
}

export function keyLevelKindLabel(kind: KeyLevelKind): string {
  switch (kind) {
    case "SWING_HIGH": return t("摆动高点", "Swing high");
    case "SWING_LOW": return t("摆动低点", "Swing low");
    case "PREV_DAY_HIGH": return t("前日高", "Previous day high");
    case "PREV_DAY_LOW": return t("前日低", "Previous day low");
    case "PREV_DAY_CLOSE": return t("前日收", "Previous day close");
    case "WEEK_OPEN": return t("周开盘", "Weekly open");
    case "MONTH_OPEN": return t("月开盘", "Monthly open");
    case "OPENING_RANGE": return t("开盘区间", "Opening range");
    case "SESSION_HIGH": return t("时段高", "Session high");
    case "SESSION_LOW": return t("时段低", "Session low");
    case "GAP": return t("缺口", "Gap");
    case "ROUND_NUMBER": return t("整数关口", "Round number");
    case "POC": return t("成交量控制点", "Point of control");
    case "VAH": return t("价值区上沿", "Value area high");
    case "VAL": return t("价值区下沿", "Value area low");
    case "ANCHORED_VWAP": return t("锚定 VWAP", "Anchored VWAP");
    case "LIQUIDATION_ZONE": return t("清算密集区", "Liquidation cluster");
  }
}

export function keyLevelStatusLabel(status: KeyLevelStatus): string {
  switch (status) {
    case "ACTIVE": return t("生效", "Active");
    case "BROKEN": return t("已破", "Broken");
    case "FLIPPED": return t("反转", "Flipped");
  }
}

export interface KeyLevelFilter {
  categories: KeyLevelCategory[];
  minStrength: number;
  /** 省略时视为观测与估算全选，兼容既有的持久化筛选状态。 */
  evidenceClasses?: EvidenceClass[];
}

export const defaultKeyLevelFilter: KeyLevelFilter = {
  categories: [...KEY_LEVEL_CATEGORIES],
  minStrength: 0.35,
  evidenceClasses: [...EVIDENCE_CLASSES],
};

export function levelEvidenceClass(level: KeyLevel): EvidenceClass {
  return level.evidence_class ?? "OBSERVED";
}

export function selectedEvidenceClasses(
  filter: KeyLevelFilter,
): readonly EvidenceClass[] {
  return filter.evidenceClasses ?? EVIDENCE_CLASSES;
}

export function levelPrice(level: KeyLevel): number {
  if (level.price != null) {
    return Number(level.price);
  }
  const low = Number(level.price_low ?? 0);
  const high = Number(level.price_high ?? low);
  return (low + high) / 2;
}

export function isZoneLevel(level: KeyLevel): boolean {
  return level.price == null && level.price_low != null && level.price_high != null;
}

export function formatLevelPrice(level: KeyLevel, precision: number): string {
  const digits = Math.min(8, Math.max(0, Math.trunc(precision)));
  if (isZoneLevel(level)) {
    const low = Number(level.price_low).toFixed(digits);
    const high = Number(level.price_high).toFixed(digits);
    return `${low}–${high}`;
  }
  return Number(level.price ?? 0).toFixed(digits);
}

export function levelTitle(level: KeyLevel, precision: number): string {
  const price = formatLevelPrice(level, precision);
  const evidence =
    levelEvidenceClass(level) === "ESTIMATED"
      ? t(" · 估算（OI 变化 + 杠杆假设）", " · estimated (OI change + leverage assumption)")
      : "";
  return `${keyLevelKindLabel(level.kind)} ${price} · ${keyLevelStatusLabel(level.status)}${evidence}`;
}

export function levelLabel(level: KeyLevel, precision: number): string {
  const price = formatLevelPrice(level, precision);
  const status = level.status === "ACTIVE" ? "" : ` · ${keyLevelStatusLabel(level.status)}`;
  const evidence = levelEvidenceClass(level) === "ESTIMATED" ? t(" · 估算", " · estimated") : "";
  return `${keyLevelKindLabel(level.kind)} ${price}${status}${evidence}`;
}

export function matchesFilter(level: KeyLevel, filter: KeyLevelFilter): boolean {
  return (
    filter.categories.includes(level.category) &&
    level.strength >= filter.minStrength &&
    selectedEvidenceClasses(filter).includes(levelEvidenceClass(level))
  );
}

/** 过滤类别/强度，并移除已忽略的 level_id；顺序稳定。 */
export function visibleKeyLevels(
  levels: readonly KeyLevel[],
  filter: KeyLevelFilter,
  ignoredIds: ReadonlySet<string>,
): KeyLevel[] {
  return levels
    .filter((level) => !ignoredIds.has(level.level_id))
    .filter((level) => matchesFilter(level, filter))
    .slice()
    .sort((left, right) => {
      if (right.strength !== left.strength) {
        return right.strength - left.strength;
      }
      return left.level_id.localeCompare(right.level_id);
    });
}

export function toggleCategory(
  filter: KeyLevelFilter,
  category: KeyLevelCategory,
): KeyLevelFilter {
  const selected = filter.categories.includes(category);
  const categories = selected
    ? filter.categories.filter((item) => item !== category)
    : [...filter.categories, category];
  return { ...filter, categories };
}

export function toggleEvidenceClass(
  filter: KeyLevelFilter,
  evidence: EvidenceClass,
): KeyLevelFilter {
  const selected = selectedEvidenceClasses(filter);
  const evidenceClasses = selected.includes(evidence)
    ? selected.filter((item) => item !== evidence)
    : [...selected, evidence];
  return { ...filter, evidenceClasses };
}

export function countByCategory(
  levels: readonly KeyLevel[],
): Record<KeyLevelCategory, number> {
  const counts: Record<KeyLevelCategory, number> = {
    STRUCTURE: 0,
    SESSION: 0,
    GAP: 0,
    ROUND_NUMBER: 0,
    VOLUME: 0,
    LIQUIDATION: 0,
  };
  for (const level of levels) {
    counts[level.category] += 1;
  }
  return counts;
}

/** 与画线标签保持同一套字号估算，保证 1440/390 的宽度判断一致。 */
function isCjk(char: string): boolean {
  return (char.codePointAt(0) ?? 0) > 0xff;
}

export function estimateKeyLevelTextWidth(text: string): number {
  let width = 8;
  for (const char of text) {
    width += isCjk(char) ? 12 : 7;
  }
  return width;
}

function fitText(text: string, maxWidth: number): string {
  if (estimateKeyLevelTextWidth(text) <= maxWidth) {
    return text;
  }
  let out = "";
  for (const char of [...text]) {
    if (estimateKeyLevelTextWidth(`${out}${char}…`) > maxWidth) {
      break;
    }
    out += char;
  }
  return out === "" ? "…" : `${out}…`;
}

export interface KeyLevelLabelInput {
  id: string;
  text: string;
  /** 关键位价格投影后的真实 y，用于画连接线。 */
  y: number;
}

export interface KeyLevelLabelFrame {
  width: number;
  height: number;
  /** 主图 pane 的上下边界；默认 0 / height。 */
  top?: number;
  bottom?: number;
  /** 右侧面板占位，标签向左让出，避免被过滤面板遮住。 */
  rightInset?: number;
}

export interface PlacedKeyLevelLabel {
  id: string;
  text: string;
  x: number;
  y: number;
  width: number;
  height: number;
  anchor: "start" | "end";
  lineY: number;
}

const LABEL_HEIGHT = 18;
const LABEL_GAP = 2;
const EDGE = 8;
const MAX_LABEL_WIDTH = 240;

/**
 * 关键位标签排布：
 * 1. 宽图贴右、窄图（<520px）贴左，避免压住价格轴；
 * 2. 文本超出可用宽度时省略，绝不出画布；
 * 3. 同一水平带内的标签依次下移，溢出时从底部整体上推，保证互不重叠。
 */
export function layoutKeyLevelLabels(
  inputs: readonly KeyLevelLabelInput[],
  frame: KeyLevelLabelFrame,
): PlacedKeyLevelLabel[] {
  if (inputs.length === 0 || frame.width <= 0 || frame.height <= 0) {
    return [];
  }
  const top = Math.max(0, frame.top ?? 0);
  const bottom = Math.min(frame.height, frame.bottom ?? frame.height);
  const rightInset = Math.max(0, frame.rightInset ?? 0);
  const preferRight = frame.width - rightInset >= 520;
  const maxWidth = Math.max(
    40,
    Math.min(MAX_LABEL_WIDTH, (frame.width - rightInset - EDGE * 2) * 0.55),
  );
  const minY = top + EDGE;
  const maxY = bottom - EDGE;

  const ordered = inputs
    .map((input) => ({ ...input }))
    .sort((left, right) => left.y - right.y || left.id.localeCompare(right.id));

  const placed: PlacedKeyLevelLabel[] = [];
  let cursor = minY;
  for (const input of ordered) {
    const text = fitText(input.text, maxWidth);
    const width = Math.min(estimateKeyLevelTextWidth(text), maxWidth);
    const anchor: "start" | "end" = preferRight ? "end" : "start";
    const x = preferRight
      ? frame.width - EDGE - rightInset
      : EDGE;

    let y = Math.max(minY, Math.min(input.y - LABEL_HEIGHT / 2, maxY - LABEL_HEIGHT));
    if (y < cursor) {
      y = cursor;
    }
    y = Math.min(y, maxY - LABEL_HEIGHT);
    cursor = y + LABEL_HEIGHT + LABEL_GAP;
    placed.push({
      id: input.id,
      text,
      x,
      y,
      width,
      height: LABEL_HEIGHT,
      anchor,
      lineY: input.y,
    });
  }

  // 底部放不下时，从最下面的标签开始整体上推，而不是裁掉。
  let floor = maxY;
  for (let index = placed.length - 1; index >= 0; index -= 1) {
    const label = placed[index];
    if (label.y + label.height > floor) {
      label.y = floor - label.height;
    }
    if (label.y < minY) {
      label.y = minY;
    }
    floor = label.y - LABEL_GAP;
  }
  return placed;
}

export function countByEvidence(
  levels: readonly KeyLevel[],
): Record<EvidenceClass, number> {
  const counts: Record<EvidenceClass, number> = {
    OBSERVED: 0,
    ESTIMATED: 0,
  };
  for (const level of levels) {
    counts[levelEvidenceClass(level)] += 1;
  }
  return counts;
}

export function statusDash(status: KeyLevelStatus): string | undefined {
  if (status === "BROKEN") {
    return "6 5";
  }
  if (status === "FLIPPED") {
    return "2 3";
  }
  return undefined;
}

/**
 * 窄屏（<=400px）进入只读视口，关键位操作必须整体隐藏；
 * 该判定集中在这里，避免面板与样式各自猜测断点。
 */
export function showLevelActions(readOnly: boolean): boolean {
  return !readOnly;
}
