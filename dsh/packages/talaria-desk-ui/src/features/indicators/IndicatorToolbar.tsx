import { useState } from "react";

import type { IndicatorBundle, IndicatorName, IndicatorSeries } from "@talaria/contracts";

import { t, useLocale, type Locale } from "../../lib/i18n";
import {
  INDICATOR_COLORS,
  INDICATOR_ORDER,
  anchorLabel,
  indicatorLabel,
  isDefaultParams,
  type IndicatorDefaults,
  type IndicatorSetting,
  type IndicatorSettings,
} from "./config";
import { describeLatest, describeParams } from "../../lib/indicatorData";

interface IndicatorToolbarProps {
  settings: IndicatorSettings;
  onChange: (name: IndicatorName, patch: Partial<IndicatorSetting>) => void;
  defaults: IndicatorDefaults;
  savingDefault: IndicatorName | null;
  onSaveDefault: (name: IndicatorName) => void;
  onResetDefault: (name: IndicatorName) => void;
  bundle: IndicatorBundle | null;
  loading: boolean;
  error: string | null;
}

function clampPeriod(value: string): number {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) {
    return 1;
  }
  return Math.min(5000, Math.max(1, parsed));
}

function paramsText(series: IndicatorSeries, locale: Locale): string {
  if (series.name === "VWAP") {
    return `VWAP(${anchorLabel(series.params.anchor === "session" ? "session" : "snapshot", locale)})`;
  }
  return describeParams(series);
}

function LegendChip({ series }: { series: IndicatorSeries }) {
  const locale = useLocale();
  const params = paramsText(series, locale);
  return (
    <span className="indicator-chip" title={`${params} ${describeLatest(series)}`}>
      <span
        className="indicator-dot"
        style={{ background: INDICATOR_COLORS[series.name] }}
        aria-hidden="true"
      />
      <strong>{indicatorLabel(series.name, locale)}</strong>
      <span className="indicator-params">{params}</span>
      <span className="indicator-latest" data-testid={`indicator-latest-${series.name}`}>
        {describeLatest(series)}
      </span>
    </span>
  );
}

export function IndicatorToolbar({
  settings,
  onChange,
  defaults,
  savingDefault,
  onSaveDefault,
  onResetDefault,
  bundle,
  loading,
  error,
}: IndicatorToolbarProps) {
  const [collapsed, setCollapsed] = useState(false);
  const locale = useLocale();
  const series = bundle?.series ?? [];

  return (
    <div className={`indicator-bar${collapsed ? " is-collapsed" : ""}`}>
      <div className="indicator-legend" data-testid="indicator-legend">
        {series.length === 0 ? (
          <span className="indicator-empty">
            {loading ? t("指标计算中…", "Calculating indicators…") : t("未启用指标", "No indicators enabled")}
          </span>
        ) : (
          series.map((item) => <LegendChip key={item.name} series={item} />)
        )}
        {loading && series.length > 0 && (
          <span className="indicator-loading" aria-live="polite">
            {t("更新中…", "Updating…")}
          </span>
        )}
        {error !== null && (
          <span className="indicator-error" role="alert" data-testid="indicator-error">
            {error}
          </span>
        )}
        {series.length > 0 && (
          <button
            type="button"
            className="legend-toggle"
            aria-expanded={!collapsed}
            onClick={() => setCollapsed((value) => !value)}
          >
            {collapsed ? t("展开参数", "Show parameters") : t("折叠参数", "Hide parameters")}
          </button>
        )}
      </div>

      <details className="indicator-settings" data-testid="indicator-settings">
        <summary>{t("指标设置", "Indicator settings")}</summary>
        <div className="indicator-grid">
          {INDICATOR_ORDER.map((name) => {
            const setting = settings[name];
            return (
              <div
                key={name}
                className={`indicator-field${setting.enabled ? " is-enabled" : ""}`}
              >
                <label className="indicator-toggle">
                  <input
                    type="checkbox"
                    checked={setting.enabled}
                    onChange={(event) =>
                      onChange(name, { enabled: event.target.checked })
                    }
                    aria-label={t(`启用 ${name}`, `Enable ${name}`)}
                  />
                  <span
                    className="indicator-dot"
                    style={{ background: INDICATOR_COLORS[name] }}
                    aria-hidden="true"
                  />
                  <strong>{indicatorLabel(name, locale)}</strong>
                </label>
                {name === "VWAP" ? (
                  <label className="indicator-param">
                    <span>{t("锚点", "Anchor")}</span>
                    <select
                      value={setting.anchor}
                      disabled={!setting.enabled}
                      onChange={(event) =>
                        onChange(name, {
                          anchor: event.target.value as "snapshot" | "session",
                        })
                      }
                    >
                      <option value="snapshot">{anchorLabel("snapshot", locale)}</option>
                      <option value="session">{anchorLabel("session", locale)}</option>
                    </select>
                  </label>
                ) : (
                  <label className="indicator-param">
                    <span>{t("周期", "Period")}</span>
                    <input
                      type="number"
                      min={1}
                      max={5000}
                      value={setting.period}
                      disabled={!setting.enabled}
                      onChange={(event) =>
                        onChange(name, { period: clampPeriod(event.target.value) })
                      }
                    />
                  </label>
                )}
                {name === "BOLL" && (
                  <label className="indicator-param">
                    <span>{t("倍数", "Multiplier")}</span>
                    <input
                      type="number"
                      min={0.1}
                      max={10}
                      step={0.1}
                      value={setting.stdDev}
                      disabled={!setting.enabled}
                      onChange={(event) => onChange(name, { stdDev: event.target.value })}
                    />
                  </label>
                )}
                <div className="indicator-default-actions">
                  <button
                    type="button"
                    className="ghost-button"
                    disabled={
                      !setting.enabled ||
                      savingDefault !== null ||
                      isDefaultParams(name, setting, defaults)
                    }
                    title={t(`把 ${name} 当前参数保存为新指标的默认值`, `Save the current ${name} parameters as the default for new indicators`)}
                    onClick={() => onSaveDefault(name)}
                  >
                    {t("设为默认", "Save as default")}
                  </button>
                  <button
                    type="button"
                    className="ghost-button"
                    disabled={!setting.enabled || savingDefault !== null}
                    title={t(`让新建的 ${name} 使用出厂参数`, `Use factory parameters for new ${name} indicators`)}
                    onClick={() => onResetDefault(name)}
                  >
                    {t("恢复出厂", "Reset to factory")}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </details>
    </div>
  );
}
