import type {
  KeyLevel,
  KeyLevelCategory,
  KeyLevelKind,
  KeyLevelStatus,
} from "@talaria/contracts";

/**
 * 关键位展示模型：过滤、价格格式化与标签避让。
 *
 * 这里全部是纯函数，像素坐标由调用方从 KLineChart 投影后传入，
 * 因此可以直接对“确认延迟可见性、贴边翻转、省略、避让”做确定性单测。
 */

export const KEY_LEVEL_CATEGORIES: readonly KeyLevelCategory[] = [
  "STRUCTURE",
  "SESSION",
  "GAP",
  "ROUND_NUMBER",
];

export const CATEGORY_META: Record<
  KeyLevelCategory,
  { label: string; color: string; description: string }
> = {
  STRUCTURE: {
    label: "结构位",
    color: "#4c8dff",
    description: "摆动高低点与多次触及区间",
  },
  SESSION: {
    label: "时段位",
    color: "#f2b84b",
    description: "前日高低收、周/月开盘与时段高低",
  },
  GAP: {
    label: "缺口",
    color: "#b07cff",
    description: "未回补跳空缺口",
  },
  ROUND_NUMBER: {
    label: "整数关口",
    color: "#48c9a9",
    description: "按价格量级取整的关口",
  },
};

export const KIND_LABELS: Record<KeyLevelKind, string> = {
  SWING_HIGH: "摆动高点",
  SWING_LOW: "摆动低点",
  PREV_DAY_HIGH: "前日高",
  PREV_DAY_LOW: "前日低",
  PREV_DAY_CLOSE: "前日收",
  WEEK_OPEN: "周开盘",
  MONTH_OPEN: "月开盘",
  OPENING_RANGE: "开盘区间",
  SESSION_HIGH: "时段高",
  SESSION_LOW: "时段低",
  GAP: "缺口",
  ROUND_NUMBER: "整数关口",
};

export const STATUS_LABELS: Record<KeyLevelStatus, string> = {
  ACTIVE: "生效",
  BROKEN: "已破",
  FLIPPED: "反转",
};

export interface KeyLevelFilter {
  categories: KeyLevelCategory[];
  minStrength: number;
}

export const defaultKeyLevelFilter: KeyLevelFilter = {
  categories: [...KEY_LEVEL_CATEGORIES],
  minStrength: 0.35,
};

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
  return `${KIND_LABELS[level.kind]} ${price} · ${STATUS_LABELS[level.status]}`;
}

export function levelLabel(level: KeyLevel, precision: number): string {
  const price = formatLevelPrice(level, precision);
  const status = level.status === "ACTIVE" ? "" : ` · ${STATUS_LABELS[level.status]}`;
  return `${KIND_LABELS[level.kind]} ${price}${status}`;
}

export function matchesFilter(level: KeyLevel, filter: KeyLevelFilter): boolean {
  return (
    filter.categories.includes(level.category) &&
    level.strength >= filter.minStrength
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

export function countByCategory(
  levels: readonly KeyLevel[],
): Record<KeyLevelCategory, number> {
  const counts: Record<KeyLevelCategory, number> = {
    STRUCTURE: 0,
    SESSION: 0,
    GAP: 0,
    ROUND_NUMBER: 0,
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

export function statusDash(status: KeyLevelStatus): string | undefined {
  if (status === "BROKEN") {
    return "6 5";
  }
  if (status === "FLIPPED") {
    return "2 3";
  }
  return undefined;
}
