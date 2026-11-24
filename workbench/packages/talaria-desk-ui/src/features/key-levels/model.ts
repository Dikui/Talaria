import type {
  EvidenceClass,
  KeyLevel,
  KeyLevelCategory,
  KeyLevelKind,
  KeyLevelStatus,
} from "@talaria/contracts";

import { t } from "../../lib/i18n";

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

export const EVIDENCE_CLASSES: readonly EvidenceClass[] = ["OBSERVED", "ESTIMATED"];

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

export function selectedEvidenceClasses(filter: KeyLevelFilter): readonly EvidenceClass[] {
  return filter.evidenceClasses ?? EVIDENCE_CLASSES;
}

export function levelPrice(level: KeyLevel): number {
  if (level.price != null) return Number(level.price);
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

export function visibleKeyLevels(
  levels: readonly KeyLevel[],
  filter: KeyLevelFilter,
  ignoredIds: ReadonlySet<string>,
): KeyLevel[] {
  return levels.filter(
    (level) => !ignoredIds.has(level.level_id) && matchesFilter(level, filter),
  );
}

export function countByCategory(levels: readonly KeyLevel[]): Record<KeyLevelCategory, number> {
  const counts: Record<KeyLevelCategory, number> = {
    STRUCTURE: 0,
    SESSION: 0,
    VOLUME: 0,
    GAP: 0,
    ROUND_NUMBER: 0,
    LIQUIDATION: 0,
  };
  for (const level of levels) {
    counts[level.category] += 1;
  }
  return counts;
}

export function countByEvidence(levels: readonly KeyLevel[]): Record<EvidenceClass, number> {
  const counts: Record<EvidenceClass, number> = { OBSERVED: 0, ESTIMATED: 0 };
  for (const level of levels) {
    counts[levelEvidenceClass(level)] += 1;
  }
  return counts;
}

export function toggleCategory(
  filter: KeyLevelFilter,
  category: KeyLevelCategory,
): KeyLevelFilter {
  const exists = filter.categories.includes(category);
  return {
    ...filter,
    categories: exists
      ? filter.categories.filter((c) => c !== category)
      : [...filter.categories, category],
  };
}

export function toggleEvidenceClass(
  filter: KeyLevelFilter,
  evidence: EvidenceClass,
): KeyLevelFilter {
  const current = selectedEvidenceClasses(filter);
  const exists = current.includes(evidence);
  return {
    ...filter,
    evidenceClasses: exists
      ? current.filter((item) => item !== evidence)
      : [...current, evidence],
  };
}
