import type {
  EvidenceClass,
  PriceScaleMode,
  StructureCategory,
  StructureFamily,
  StructureFeature,
  StructureStatus,
} from "@talaria/contracts";

import { t } from "../../lib/i18n";

export const STRUCTURE_FAMILIES: readonly StructureFamily[] = [
  "CONFLUENCE",
  "TRENDLINE",
  "LEVEL",
  "ZONE",
  "CHANNEL",
];

export const STRUCTURE_CATEGORIES: readonly StructureCategory[] = [
  "CONFLUENCE",
  "TREND",
  "STRUCTURE",
  "SESSION",
  "GAP",
  "ROUND_NUMBER",
  "VOLUME",
  "LIQUIDATION",
];

export const STRUCTURE_EVIDENCE_CLASSES: readonly EvidenceClass[] = ["OBSERVED", "ESTIMATED"];

export const MTFA_CANDIDATE_TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d"] as const;

const TF_MINUTES: Record<string, number> = {
  "1m": 1,
  "5m": 5,
  "15m": 15,
  "1h": 60,
  "4h": 240,
  "1d": 1440,
};

export function higherTimeframesFor(baseTimeframe: string): string[] {
  const baseMin = TF_MINUTES[baseTimeframe] ?? 1;
  return MTFA_CANDIDATE_TIMEFRAMES.filter((tf) => (TF_MINUTES[tf] ?? 0) > baseMin);
}

export type StructureViewMode = "simple" | "expert";

export interface StructureFilter {
  enabled: boolean;
  viewMode: StructureViewMode;
  scale: PriceScaleMode;
  htfTimeframes: string[];
  families: StructureFamily[];
  categories: StructureCategory[];
  detectorIds: string[];
  evidenceClasses: EvidenceClass[];
  minStrength: number;
}

export const defaultStructureFilter: StructureFilter = {
  enabled: false,
  viewMode: "simple",
  scale: "LINEAR",
  htfTimeframes: [],
  families: [...STRUCTURE_FAMILIES],
  categories: [...STRUCTURE_CATEGORIES],
  detectorIds: [],
  evidenceClasses: [...STRUCTURE_EVIDENCE_CLASSES],
  minStrength: 0.35,
};

export function isStructuresEnabled(filter: StructureFilter): boolean {
  return filter.enabled;
}

export function toggleStructuresEnabled(filter: StructureFilter): StructureFilter {
  return { ...filter, enabled: !filter.enabled };
}

export interface FamilyMeta {
  label: string;
  color: string;
}

export function familyMeta(family: StructureFamily): FamilyMeta {
  switch (family) {
    case "CONFLUENCE":
      return { label: t("共振带", "Confluence"), color: "#f2b84b" };
    case "TRENDLINE":
      return { label: t("趋势线", "Trendline"), color: "#3dd6b5" };
    case "LEVEL":
      return { label: t("水平位", "Level"), color: "#4c8dff" };
    case "ZONE":
      return { label: t("价格带", "Zone"), color: "#b07cff" };
    case "CHANNEL":
      return { label: t("通道", "Channel"), color: "#ff9e57" };
  }
}

export function structureCategoryMeta(category: StructureCategory): { label: string; color: string } {
  switch (category) {
    case "CONFLUENCE":
      return { label: t("多维共振", "Confluence"), color: "#f2b84b" };
    case "TREND":
      return { label: t("趋势结构", "Trend"), color: "#3dd6b5" };
    case "STRUCTURE":
      return { label: t("摆动结构", "Swing"), color: "#4c8dff" };
    case "SESSION":
      return { label: t("时段位", "Session"), color: "#e8b44c" };
    case "GAP":
      return { label: t("缺口/FVG", "Gap/FVG"), color: "#b07cff" };
    case "ROUND_NUMBER":
      return { label: t("整数关口", "Round"), color: "#48c9a9" };
    case "VOLUME":
      return { label: t("量价位", "Volume"), color: "#e58f65" };
    case "LIQUIDATION":
      return { label: t("清算区", "Liquidation"), color: "#ff6b7a" };
  }
}

export function structureStatusLabel(status: StructureStatus): string {
  switch (status) {
    case "ACTIVE":
      return t("生效", "Active");
    case "BROKEN":
      return t("已破", "Broken");
    case "FLIPPED":
      return t("反转", "Flipped");
    case "EXPIRED":
      return t("过期", "Expired");
  }
}

export function structureEvidenceClass(feature: StructureFeature): EvidenceClass {
  return feature.evidence_class ?? "OBSERVED";
}

export function structureMidPrice(feature: StructureFeature): number {
  const g = feature.geometry;
  if ("price" in g) return Number(g.price);
  if ("price_low" in g) return (Number(g.price_low) + Number(g.price_high)) / 2;
  if ("anchor_end" in g) return Number(g.anchor_end.price);
  return (Number(g.upper.anchor_end.price) + Number(g.lower.anchor_end.price)) / 2;
}

export function formatStructureGeometry(feature: StructureFeature, precision: number): string {
  const digits = Math.min(8, Math.max(0, Math.trunc(precision)));
  const g = feature.geometry;
  if ("price" in g) return Number(g.price).toFixed(digits);
  if ("price_low" in g) {
    return `${Number(g.price_low).toFixed(digits)}–${Number(g.price_high).toFixed(digits)}`;
  }
  if ("anchor_start" in g) {
    return `${Number(g.anchor_start.price).toFixed(digits)} → ${Number(g.anchor_end.price).toFixed(digits)}`;
  }
  return `${Number(g.lower.anchor_end.price).toFixed(digits)}–${Number(g.upper.anchor_end.price).toFixed(digits)}`;
}

export function structureLabel(feature: StructureFeature, precision: number): string {
  const tfPrefix =
    feature.source_timeframe && feature.source_timeframe !== feature.timeframe
      ? `[${feature.source_timeframe}] `
      : "";
  const geom = formatStructureGeometry(feature, precision);
  const status = feature.status === "ACTIVE" ? "" : ` · ${structureStatusLabel(feature.status)}`;
  const est = structureEvidenceClass(feature) === "ESTIMATED" ? t(" · 估算", " · est") : "";
  return `${tfPrefix}${feature.kind} ${geom}${status}${est}`;
}

export function visibleStructures(
  structures: readonly StructureFeature[],
  filter: StructureFilter,
  ignoredIds: ReadonlySet<string>,
): StructureFeature[] {
  if (!filter.enabled) return [];
  const active = structures.filter((item) => !ignoredIds.has(item.structure_id));

  if (filter.viewMode === "simple") {
    const confluence = active
      .filter((s) => s.family === "CONFLUENCE" && s.strength >= filter.minStrength)
      .sort((a, b) => b.strength - a.strength)
      .slice(0, 6);
    const trendlines = active
      .filter((s) => s.family === "TRENDLINE" && s.strength >= filter.minStrength)
      .sort((a, b) => b.strength - a.strength)
      .slice(0, 4);
    const simpleSet = [...confluence, ...trendlines];
    if (simpleSet.length > 0) return simpleSet;
    return active
      .filter((s) => s.strength >= filter.minStrength)
      .sort((a, b) => b.strength - a.strength)
      .slice(0, 6);
  }

  return active.filter((item) => {
    if (!filter.families.includes(item.family)) return false;
    if (!filter.categories.includes(item.category)) return false;
    if (!filter.evidenceClasses.includes(structureEvidenceClass(item))) return false;
    if (item.strength < filter.minStrength) return false;
    if (filter.detectorIds.length > 0 && !filter.detectorIds.includes(item.detector_id)) {
      return false;
    }
    return true;
  });
}

export function countByFamily(
  structures: readonly StructureFeature[],
): Record<StructureFamily, number> {
  const counts: Record<StructureFamily, number> = {
    CONFLUENCE: 0,
    TRENDLINE: 0,
    LEVEL: 0,
    ZONE: 0,
    CHANNEL: 0,
  };
  for (const s of structures) {
    counts[s.family] += 1;
  }
  return counts;
}

export function toggleFamily(filter: StructureFilter, family: StructureFamily): StructureFilter {
  const exists = filter.families.includes(family);
  return {
    ...filter,
    families: exists ? filter.families.filter((f) => f !== family) : [...filter.families, family],
  };
}

export function toggleStructureCategory(
  filter: StructureFilter,
  category: StructureCategory,
): StructureFilter {
  const exists = filter.categories.includes(category);
  return {
    ...filter,
    categories: exists
      ? filter.categories.filter((c) => c !== category)
      : [...filter.categories, category],
  };
}

export function toggleHtfTimeframe(filter: StructureFilter, tf: string): StructureFilter {
  const exists = filter.htfTimeframes.includes(tf);
  return {
    ...filter,
    htfTimeframes: exists
      ? filter.htfTimeframes.filter((t) => t !== tf)
      : [...filter.htfTimeframes, tf],
  };
}

export function toggleDetectorId(filter: StructureFilter, detectorId: string): StructureFilter {
  const exists = filter.detectorIds.includes(detectorId);
  return {
    ...filter,
    detectorIds: exists
      ? filter.detectorIds.filter((id) => id !== detectorId)
      : [...filter.detectorIds, detectorId],
  };
}

export function toggleStructureEvidence(
  filter: StructureFilter,
  evidence: EvidenceClass,
): StructureFilter {
  const exists = filter.evidenceClasses.includes(evidence);
  return {
    ...filter,
    evidenceClasses: exists
      ? filter.evidenceClasses.filter((e) => e !== evidence)
      : [...filter.evidenceClasses, evidence],
  };
}
