import type { IndicatorName, IndicatorParams } from "@talaria/contracts";

/** 与后端 operators/spec.py 的默认值一一对应；前端只做展示与参数收集。 */
export const INDICATOR_ORDER: IndicatorName[] = [
  "EMA",
  "SMA",
  "BOLL",
  "VWAP",
  "RSI",
  "ATR",
];

export const INDICATOR_DEFAULTS: Record<
  IndicatorName,
  { period: number; stdDev: string; anchor: "snapshot" | "session" }
> = {
  EMA: { period: 20, stdDev: "2", anchor: "snapshot" },
  SMA: { period: 20, stdDev: "2", anchor: "snapshot" },
  BOLL: { period: 20, stdDev: "2", anchor: "snapshot" },
  VWAP: { period: 1, stdDev: "2", anchor: "snapshot" },
  RSI: { period: 14, stdDev: "2", anchor: "snapshot" },
  ATR: { period: 14, stdDev: "2", anchor: "snapshot" },
};

export const INDICATOR_CHANNELS: Record<IndicatorName, string[]> = {
  EMA: ["value"],
  SMA: ["value"],
  BOLL: ["mid", "upper", "lower"],
  VWAP: ["value"],
  RSI: ["value"],
  ATR: ["value"],
};

export const INDICATOR_COLORS: Record<IndicatorName, string> = {
  EMA: "#d97706",
  SMA: "#2563eb",
  BOLL: "#7c3aed",
  VWAP: "#0f766e",
  RSI: "#db2777",
  ATR: "#0891b2",
};

export interface IndicatorSetting {
  enabled: boolean;
  period: number;
  stdDev: string;
  anchor: "snapshot" | "session";
}

export type IndicatorSettings = Record<IndicatorName, IndicatorSetting>;

export const DEFAULT_ENABLED: IndicatorName[] = ["EMA", "RSI", "ATR"];

export function defaultIndicatorSettings(): IndicatorSettings {
  const settings = {} as IndicatorSettings;
  for (const name of INDICATOR_ORDER) {
    settings[name] = {
      enabled: DEFAULT_ENABLED.includes(name),
      ...INDICATOR_DEFAULTS[name],
    };
  }
  return settings;
}

export function enabledIndicatorNames(
  settings: IndicatorSettings,
): IndicatorName[] {
  return INDICATOR_ORDER.filter((name) => settings[name].enabled);
}

export function isDefaultParams(
  name: IndicatorName,
  setting: IndicatorSetting,
): boolean {
  const defaults = INDICATOR_DEFAULTS[name];
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

/** GET 只服务默认参数；任一启用指标非默认时整批走 POST compute。 */
export function needsOperatorRequest(
  names: IndicatorName[],
  settings: IndicatorSettings,
): boolean {
  return names.some((name) => !isDefaultParams(name, settings[name]));
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

export function settingsSignature(
  names: IndicatorName[],
  settings: IndicatorSettings,
): string {
  return names
    .map((name) => {
      const setting = settings[name];
      return [
        name,
        setting.period,
        name === "BOLL" ? setting.stdDev : "",
        name === "VWAP" ? setting.anchor : "",
      ].join(":");
    })
    .join("|");
}
