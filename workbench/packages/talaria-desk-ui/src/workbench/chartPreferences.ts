import type { EvidenceClass, KeyLevelCategory } from "@talaria/contracts";

import {
  INDICATOR_ORDER,
  defaultIndicatorSettings,
  isDefaultParams,
  type IndicatorDefaults,
  type IndicatorSetting,
  type IndicatorSettings,
} from "../features/indicators/config";
import {
  EVIDENCE_CLASSES,
  KEY_LEVEL_CATEGORIES,
  defaultKeyLevelFilter,
  type KeyLevelFilter,
} from "../features/key-levels/model";
import { isTauriRuntime } from "../lib/tauriBridge";

export const INDICATOR_PREFS_STORAGE_KEY = "talaria.workbench.indicators.v1";
export const KEY_LEVEL_PREFS_STORAGE_KEY = "talaria.workbench.keyLevels.v1";

const LOCAL_HOSTNAMES = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);

export interface PreferenceEnvOptions {
  storage?: Pick<Storage, "getItem" | "setItem"> | null;
  isLocal?: boolean;
}

export function isLocalWorkbenchRuntime(
  loc: Pick<Location, "hostname"> | undefined = typeof window !== "undefined"
    ? window.location
    : undefined,
  tauri: boolean = isTauriRuntime(),
): boolean {
  if (tauri) return true;
  if (!loc?.hostname) return false;
  return LOCAL_HOSTNAMES.has(loc.hostname.toLowerCase());
}

function resolveStorage(
  options?: PreferenceEnvOptions,
): Pick<Storage, "getItem" | "setItem"> | null {
  const isLocal = options?.isLocal ?? isLocalWorkbenchRuntime();
  if (!isLocal) return null;
  if (options && "storage" in options) {
    return options.storage ?? null;
  }
  if (typeof localStorage !== "undefined") {
    return localStorage;
  }
  return null;
}

export function loadIndicatorSettings(
  defaults: IndicatorDefaults,
  options?: PreferenceEnvOptions,
): IndicatorSettings {
  const base = defaultIndicatorSettings(defaults);
  const storage = resolveStorage(options);
  if (!storage) return base;

  try {
    const raw = storage.getItem(INDICATOR_PREFS_STORAGE_KEY);
    if (!raw) return base;
    const parsed = JSON.parse(raw) as Partial<Record<string, Partial<IndicatorSetting>>>;
    if (!parsed || typeof parsed !== "object") return base;

    const result = { ...base };
    for (const name of INDICATOR_ORDER) {
      const saved = parsed[name];
      if (!saved || typeof saved !== "object") continue;
      const fallback = base[name];
      result[name] = {
        enabled: typeof saved.enabled === "boolean" ? saved.enabled : fallback.enabled,
        period:
          typeof saved.period === "number" && Number.isFinite(saved.period) && saved.period >= 1
            ? Math.min(5000, Math.trunc(saved.period))
            : fallback.period,
        stdDev:
          typeof saved.stdDev === "string" && saved.stdDev.trim().length > 0
            ? saved.stdDev
            : fallback.stdDev,
        anchor:
          saved.anchor === "session" || saved.anchor === "snapshot"
            ? saved.anchor
            : fallback.anchor,
      };
    }
    return result;
  } catch {
    return base;
  }
}

export function saveIndicatorSettings(
  settings: IndicatorSettings,
  options?: PreferenceEnvOptions,
): void {
  const storage = resolveStorage(options);
  if (!storage) return;
  try {
    storage.setItem(INDICATOR_PREFS_STORAGE_KEY, JSON.stringify(settings));
  } catch {
    // Ignore storage quota / privacy mode errors.
  }
}

export function mergeIndicatorDefaults(
  prevSettings: IndicatorSettings,
  prevDefaults: IndicatorDefaults,
  nextDefaults: IndicatorDefaults,
): IndicatorSettings {
  const next = {} as IndicatorSettings;
  for (const name of INDICATOR_ORDER) {
    const current = prevSettings[name];
    const useNextDefault = isDefaultParams(name, current, prevDefaults);
    next[name] = useNextDefault
      ? { ...nextDefaults[name], enabled: current.enabled }
      : { ...current };
  }
  return next;
}

export function loadKeyLevelFilter(options?: PreferenceEnvOptions): KeyLevelFilter {
  const base: KeyLevelFilter = {
    enabled: defaultKeyLevelFilter.enabled,
    categories: [...defaultKeyLevelFilter.categories],
    minStrength: defaultKeyLevelFilter.minStrength,
    evidenceClasses: [...(defaultKeyLevelFilter.evidenceClasses ?? EVIDENCE_CLASSES)],
  };
  const storage = resolveStorage(options);
  if (!storage) return base;

  try {
    const raw = storage.getItem(KEY_LEVEL_PREFS_STORAGE_KEY);
    if (!raw) return base;
    const parsed = JSON.parse(raw) as Partial<KeyLevelFilter>;
    if (!parsed || typeof parsed !== "object") return base;

    const validCategories = new Set<string>(KEY_LEVEL_CATEGORIES);
    const validEvidence = new Set<string>(EVIDENCE_CLASSES);

    const categories = Array.isArray(parsed.categories)
      ? parsed.categories.filter(
          (c): c is KeyLevelCategory => typeof c === "string" && validCategories.has(c),
        )
      : base.categories;

    const evidenceClasses = Array.isArray(parsed.evidenceClasses)
      ? parsed.evidenceClasses.filter(
          (e): e is EvidenceClass => typeof e === "string" && validEvidence.has(e),
        )
      : base.evidenceClasses;

    const minStrength =
      typeof parsed.minStrength === "number" && Number.isFinite(parsed.minStrength)
        ? Math.min(1, Math.max(0, parsed.minStrength))
        : base.minStrength;

    const enabled = typeof parsed.enabled === "boolean" ? parsed.enabled : base.enabled;

    return {
      enabled,
      categories,
      minStrength,
      evidenceClasses,
    };
  } catch {
    return base;
  }
}

export function saveKeyLevelFilter(
  filter: KeyLevelFilter,
  options?: PreferenceEnvOptions,
): void {
  const storage = resolveStorage(options);
  if (!storage) return;
  try {
    storage.setItem(KEY_LEVEL_PREFS_STORAGE_KEY, JSON.stringify(filter));
  } catch {
    // Ignore storage quota / privacy mode errors.
  }
}
