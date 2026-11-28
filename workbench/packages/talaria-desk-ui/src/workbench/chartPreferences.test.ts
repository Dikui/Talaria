import { describe, expect, it } from "vitest";
import type { KeyLevel } from "@talaria/contracts";

import {
  INDICATOR_DEFAULTS,
  INDICATOR_ORDER,
  defaultIndicatorSettings,
  updateIndicatorSetting,
} from "../features/indicators/config";
import {
  defaultKeyLevelFilter,
  isKeyLevelsEnabled,
  toggleKeyLevelsEnabled,
  visibleKeyLevels,
} from "../features/key-levels/model";
import {
  defaultStructureFilter,
  toggleDetectorId,
} from "../features/structures/model";
import {
  INDICATOR_PREFS_STORAGE_KEY,
  KEY_LEVEL_PREFS_STORAGE_KEY,
  STRUCTURE_PREFS_STORAGE_KEY,
  isLocalWorkbenchRuntime,
  loadIndicatorSettings,
  loadKeyLevelFilter,
  loadStructureFilter,
  mergeIndicatorDefaults,
  saveIndicatorSettings,
  saveKeyLevelFilter,
  saveStructureFilter,
} from "./chartPreferences";

function createMemoryStorage(): Pick<Storage, "getItem" | "setItem"> & {
  data: Map<string, string>;
} {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => {
      data.set(key, value);
    },
  };
}

const sampleLevel: KeyLevel = {
  level_id: "lvl-1",
  instrument_id: "BTC-USDT-PERP",
  timeframe: "4h",
  category: "STRUCTURE",
  kind: "SWING_HIGH",
  price: "85000",
  strength: 0.8,
  touch_count: 2,
  status: "ACTIVE",
  evidence_class: "OBSERVED",
  ts_available: 1_700_000_000_000,
  formed_ts: 1_700_000_000_000,
  volume_weight: "100",
};

describe("chartPreferences", () => {
  it("detects local vs cloud runtime", () => {
    expect(isLocalWorkbenchRuntime({ hostname: "localhost" }, false)).toBe(true);
    expect(isLocalWorkbenchRuntime({ hostname: "127.0.0.1" }, false)).toBe(true);
    expect(isLocalWorkbenchRuntime({ hostname: "[::1]" }, false)).toBe(true);
    expect(isLocalWorkbenchRuntime({ hostname: "desk.talaria.io" }, true)).toBe(true);
    expect(isLocalWorkbenchRuntime({ hostname: "desk.talaria.io" }, false)).toBe(false);
    expect(isLocalWorkbenchRuntime({ hostname: "198.51.100.5" }, false)).toBe(false);
  });

  it("defaults all indicators and key levels to disabled", () => {
    const indicators = defaultIndicatorSettings(INDICATOR_DEFAULTS);
    for (const name of INDICATOR_ORDER) {
      expect(indicators[name].enabled).toBe(false);
    }

    expect(isKeyLevelsEnabled(defaultKeyLevelFilter)).toBe(false);
    expect(visibleKeyLevels([sampleLevel], defaultKeyLevelFilter, new Set())).toEqual([]);

    const enabledFilter = toggleKeyLevelsEnabled(defaultKeyLevelFilter);
    expect(isKeyLevelsEnabled(enabledFilter)).toBe(true);
    expect(visibleKeyLevels([sampleLevel], enabledFilter, new Set())).toEqual([sampleLevel]);
  });

  it("persists indicator and key-level choices in local runtime", () => {
    const storage = createMemoryStorage();
    const updatedIndicators = updateIndicatorSetting(
      defaultIndicatorSettings(INDICATOR_DEFAULTS),
      INDICATOR_DEFAULTS,
      "EMA",
      { enabled: true, period: 21 },
    );
    saveIndicatorSettings(updatedIndicators, { isLocal: true, storage });

    const reloadedIndicators = loadIndicatorSettings(INDICATOR_DEFAULTS, {
      isLocal: true,
      storage,
    });
    expect(reloadedIndicators.EMA.enabled).toBe(true);
    expect(reloadedIndicators.EMA.period).toBe(21);
    expect(reloadedIndicators.RSI.enabled).toBe(false);

    const updatedLevels = {
      ...toggleKeyLevelsEnabled(defaultKeyLevelFilter),
      minStrength: 0.6,
    };
    saveKeyLevelFilter(updatedLevels, { isLocal: true, storage });

    const reloadedLevels = loadKeyLevelFilter({ isLocal: true, storage });
    expect(reloadedLevels.enabled).toBe(true);
    expect(reloadedLevels.minStrength).toBe(0.6);
  });

  it("always starts disabled and never persists in cloud runtime", () => {
    const storage = createMemoryStorage();
    const enabledIndicators = updateIndicatorSetting(
      defaultIndicatorSettings(INDICATOR_DEFAULTS),
      INDICATOR_DEFAULTS,
      "RSI",
      { enabled: true },
    );
    const enabledLevels = toggleKeyLevelsEnabled(defaultKeyLevelFilter);

    // Pre-populate storage as if local preferences existed.
    storage.setItem(INDICATOR_PREFS_STORAGE_KEY, JSON.stringify(enabledIndicators));
    storage.setItem(KEY_LEVEL_PREFS_STORAGE_KEY, JSON.stringify(enabledLevels));

    const cloudIndicators = loadIndicatorSettings(INDICATOR_DEFAULTS, {
      isLocal: false,
      storage,
    });
    expect(cloudIndicators.RSI.enabled).toBe(false);

    const cloudLevels = loadKeyLevelFilter({ isLocal: false, storage });
    expect(cloudLevels.enabled).toBe(false);

    // Saving in cloud runtime should be a no-op.
    const cloudStorage = createMemoryStorage();
    saveIndicatorSettings(enabledIndicators, { isLocal: false, storage: cloudStorage });
    saveKeyLevelFilter(enabledLevels, { isLocal: false, storage: cloudStorage });
    expect(cloudStorage.data.size).toBe(0);
  });

  it("preserves user toggles and custom params when server indicator defaults arrive", () => {
    const prevDefaults = INDICATOR_DEFAULTS;
    const prevSettings = updateIndicatorSetting(
      updateIndicatorSetting(
        defaultIndicatorSettings(prevDefaults),
        prevDefaults,
        "EMA",
        { enabled: true, period: 55 },
      ),
      prevDefaults,
      "RSI",
      { enabled: true },
    );

    const nextDefaults = {
      ...prevDefaults,
      EMA: { ...prevDefaults.EMA, period: 12 },
      RSI: { ...prevDefaults.RSI, period: 7 },
    };

    const merged = mergeIndicatorDefaults(prevSettings, prevDefaults, nextDefaults);
    // Custom EMA period is preserved, enabled stays true.
    expect(merged.EMA.enabled).toBe(true);
    expect(merged.EMA.period).toBe(55);
    // Non-custom RSI period picks up server default, enabled stays true.
    expect(merged.RSI.enabled).toBe(true);
    expect(merged.RSI.period).toBe(7);
    // Unenabled indicator remains disabled.
    expect(merged.ATR.enabled).toBe(false);
  });

  it("persists disabledDetectorIds in local runtime and resets in cloud runtime", () => {
    const storage = createMemoryStorage();
    const updated = toggleDetectorId(defaultStructureFilter, "pivot_trendline@1");
    saveStructureFilter(updated, { isLocal: true, storage });

    const reloadedLocal = loadStructureFilter({ isLocal: true, storage });
    expect(reloadedLocal.disabledDetectorIds).toEqual(["pivot_trendline@1"]);

    expect(storage.getItem(STRUCTURE_PREFS_STORAGE_KEY)).toContain("pivot_trendline@1");

    const reloadedCloud = loadStructureFilter({ isLocal: false, storage });
    expect(reloadedCloud.disabledDetectorIds).toEqual([]);
  });
});
