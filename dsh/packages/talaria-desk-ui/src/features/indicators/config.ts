import type { IndicatorName, IndicatorParams, OperatorRequest } from "@talaria/contracts";

/** 与后端 operators/spec.py 的默认值一一对应；前端只做展示与参数收集。 */
export const INDICATOR_ORDER: IndicatorName[] = [
  "EMA",
  "SMA",
  "BOLL",
  "VWAP",
  "RSI",
  "ATR",
  "FUNDING_QUANTILE",
  "OI_DIVERGENCE",
  "NET_ODDS",
];

export interface IndicatorDefaultValues {
  period: number;
  stdDev: string;
  anchor: "snapshot" | "session";
}

export type IndicatorDefaults = Record<IndicatorName, IndicatorDefaultValues>;
export type ServerIndicatorDefaults = Partial<
  Record<IndicatorName, IndicatorParams>
>;

/** 出厂值；与后端 operators/spec.py 的 DEFAULT_PERIODS 一一对应。 */
export const INDICATOR_DEFAULTS: IndicatorDefaults = {
  EMA: { period: 9, stdDev: "2", anchor: "snapshot" },
  SMA: { period: 9, stdDev: "2", anchor: "snapshot" },
  BOLL: { period: 20, stdDev: "2", anchor: "snapshot" },
  VWAP: { period: 1, stdDev: "2", anchor: "session" },
  RSI: { period: 14, stdDev: "2", anchor: "snapshot" },
  ATR: { period: 14, stdDev: "2", anchor: "snapshot" },
  FUNDING_QUANTILE: { period: 30, stdDev: "2", anchor: "snapshot" },
  OI_DIVERGENCE: { period: 30, stdDev: "2", anchor: "snapshot" },
  NET_ODDS: { period: 20, stdDev: "2", anchor: "snapshot" },
};

/** 服务端“生效默认值”归一化为前端设置形状；缺省项回落到出厂值。 */
export function defaultsFromParams(
  params: ServerIndicatorDefaults,
): IndicatorDefaults {
  const defaults = {} as IndicatorDefaults;
  for (const name of INDICATOR_ORDER) {
    const fallback = INDICATOR_DEFAULTS[name];
    const value = params[name];
    defaults[name] = {
      period: value?.period ?? fallback.period,
      stdDev:
        value?.std_dev == null ? fallback.stdDev : String(value.std_dev),
      anchor: value?.anchor ?? fallback.anchor,
    };
  }
  return defaults;
}

export const INDICATOR_CHANNELS: Record<IndicatorName, string[]> = {
  EMA: ["value"],
  SMA: ["value"],
  BOLL: ["mid", "upper", "lower"],
  VWAP: ["value"],
  RSI: ["value"],
  ATR: ["value"],
  FUNDING_QUANTILE: ["value"],
  OI_DIVERGENCE: ["value"],
  NET_ODDS: ["value"],
};

export const INDICATOR_COLORS: Record<IndicatorName, string> = {
  EMA: "#d97706",
  SMA: "#2563eb",
  BOLL: "#7c3aed",
  VWAP: "#0f766e",
  RSI: "#db2777",
  ATR: "#0891b2",
  FUNDING_QUANTILE: "#c2410c",
  OI_DIVERGENCE: "#4d7c0f",
  NET_ODDS: "#7e22ce",
};

export const INDICATOR_LABELS: Partial<Record<IndicatorName, string>> = {
  FUNDING_QUANTILE: "资金费率分位",
  OI_DIVERGENCE: "OI 背离",
  NET_ODDS: "净赔率",
};

export interface IndicatorSetting {
  enabled: boolean;
  period: number;
  stdDev: string;
  anchor: "snapshot" | "session";
}

export type IndicatorSettings = Record<IndicatorName, IndicatorSetting>;

export const DEFAULT_ENABLED: IndicatorName[] = ["EMA", "RSI", "ATR"];

export function defaultIndicatorSettings(
  defaults: IndicatorDefaults = INDICATOR_DEFAULTS,
): IndicatorSettings {
  const settings = {} as IndicatorSettings;
  for (const name of INDICATOR_ORDER) {
    settings[name] = {
      enabled: DEFAULT_ENABLED.includes(name),
      ...defaults[name],
    };
  }
  return settings;
}

export function updateIndicatorSetting(
  settings: IndicatorSettings,
  defaults: IndicatorDefaults,
  name: IndicatorName,
  patch: Partial<IndicatorSetting>,
): IndicatorSettings {
  const current = settings[name];
  const params = patch.enabled && !current.enabled ? defaults[name] : current;
  return { ...settings, [name]: { ...current, ...params, ...patch } };
}

export function enabledIndicatorNames(
  settings: IndicatorSettings,
): IndicatorName[] {
  return INDICATOR_ORDER.filter((name) => settings[name].enabled);
}

export function isDefaultParams(
  name: IndicatorName,
  setting: IndicatorSetting,
  defaultsMap: IndicatorDefaults = INDICATOR_DEFAULTS,
): boolean {
  const defaults = defaultsMap[name];
  if (setting.period !== defaults.period) {
    return false;
  }
  if (name === "BOLL" && Number(setting.stdDev) !== Number(defaults.stdDev)) {
    return false;
  }
  if (name === "VWAP" && setting.anchor !== defaults.anchor) {
    return false;
  }
  return true;
}

export function operatorParams(
  name: IndicatorName,
  setting: IndicatorSetting,
): IndicatorParams {
  const params: IndicatorParams = { period: setting.period };
  if (name === "BOLL") {
    params.std_dev = setting.stdDev;
  }
  if (name === "VWAP") {
    params.anchor = setting.anchor;
  }
  return params;
}

export function anchorLabel(anchor: "snapshot" | "session"): string {
  return anchor === "session" ? "UTC 日界" : "快照起点";
}

export function indicatorRequests(
  settings: IndicatorSettings | null,
  snapshotId: string,
  timeframe: string,
  limit: number,
): OperatorRequest[] {
  if (settings === null) {
    return [];
  }
  return enabledIndicatorNames(settings).map((name) => ({
    snapshot_id: snapshotId,
    timeframe,
    operator: name,
    params: operatorParams(name, settings[name]),
    limit,
  }));
}
