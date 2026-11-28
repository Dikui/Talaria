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
  disabledDetectorIds: string[];
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
  disabledDetectorIds: [],
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

const DETECTOR_LABELS: Record<string, readonly [zh: string, en: string]> = {
  confluence: ["多维共振带", "Confluence zones"],
  convex_envelope: ["凸包包络线", "Convex envelope"],
  equal_levels: ["等高/等低位", "Equal levels"],
  fvg: ["FVG 缺口", "Fair value gap"],
  gap_levels: ["跳空缺口", "Price gaps"],
  liquidation_zones: ["清算估算区", "Liquidation zones"],
  pivot_trendline: ["摆动趋势线", "Pivot trendline"],
  round_numbers: ["整数关口", "Round numbers"],
  session_levels: ["时段关键位", "Session levels"],
  swing_levels: ["摆动高低位", "Swing levels"],
  volume_profile: ["量价/VWAP", "Volume profile"],
};

export function parseDetectorId(detectorId: string): {
  base: string;
  version: string | null;
} {
  const atIdx = detectorId.lastIndexOf("@");
  if (atIdx <= 0) {
    return { base: detectorId, version: null };
  }
  const base = detectorId.slice(0, atIdx);
  const version = detectorId.slice(atIdx + 1) || null;
  return { base, version };
}

export function detectorBaseLabel(detectorId: string): string {
  const { base } = parseDetectorId(detectorId);
  const entry = DETECTOR_LABELS[base];
  return entry ? t(entry[0], entry[1]) : base;
}

export function detectorVersionBadge(detectorId: string): string | null {
  const { version } = parseDetectorId(detectorId);
  return version ? `v${version}` : null;
}

export function detectorLabel(detectorId: string, catalogIds?: readonly string[]): string {
  const { base, version } = parseDetectorId(detectorId);
  const entry = DETECTOR_LABELS[base];
  if (!entry) return detectorId;
  const label = t(entry[0], entry[1]);
  const hasMultipleVersions =
    catalogIds !== undefined &&
    new Set(catalogIds.filter((id) => parseDetectorId(id).base === base)).size >= 2;
  return version && (version !== "1" || hasMultipleVersions) ? `${label} v${version}` : label;
}

function numericVersion(version: string | null): number {
  if (!version) return Number.POSITIVE_INFINITY;
  const num = Number(version);
  return Number.isFinite(num) && /^\d+(?:\.\d+)?$/.test(version)
    ? num
    : Number.POSITIVE_INFINITY;
}

export function catalogDetectors(
  registeredIds: readonly string[],
  structures: readonly StructureFeature[],
): string[] {
  const set = new Set<string>(registeredIds);
  if (registeredIds.length > 0) {
    set.add("confluence@1");
  }
  for (const item of structures) {
    if (item.detector_id) set.add(item.detector_id);
  }
  return Array.from(set).sort((a, b) => {
    const pa = parseDetectorId(a);
    const pb = parseDetectorId(b);
    if (pa.base !== pb.base) return pa.base.localeCompare(pb.base);
    const va = numericVersion(pa.version);
    const vb = numericVersion(pb.version);
    if (va !== vb) return va - vb;
    return a.localeCompare(b);
  });
}

export function countByDetector(
  structures: readonly StructureFeature[],
): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const s of structures) {
    counts[s.detector_id] = (counts[s.detector_id] ?? 0) + 1;
  }
  return counts;
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
  const active = structures.filter(
    (item) => !ignoredIds.has(item.structure_id) && isDetectorEnabled(filter, item.detector_id),
  );

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

export function isDetectorEnabled(filter: StructureFilter, detectorId: string): boolean {
  return !filter.disabledDetectorIds.includes(detectorId);
}

export function toggleDetectorId(filter: StructureFilter, detectorId: string): StructureFilter {
  const disabled = filter.disabledDetectorIds.includes(detectorId);
  return {
    ...filter,
    disabledDetectorIds: disabled
      ? filter.disabledDetectorIds.filter((id) => id !== detectorId)
      : [...filter.disabledDetectorIds, detectorId],
  };
}

export function enableAllDetectors(filter: StructureFilter): StructureFilter {
  return { ...filter, disabledDetectorIds: [] };
}

export function disableAllDetectors(
  filter: StructureFilter,
  catalog: readonly string[],
): StructureFilter {
  return { ...filter, disabledDetectorIds: Array.from(new Set(catalog)) };
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
