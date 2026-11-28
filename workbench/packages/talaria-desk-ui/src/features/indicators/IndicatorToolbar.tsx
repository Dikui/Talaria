import { useState } from "react";
import { Layers } from "lucide-react";
import type { IndicatorBundle, IndicatorName, IndicatorSeries } from "@talaria/contracts";

import { t, useLocale } from "../../lib/i18n";
import { describeLatest, describeParams } from "../../lib/indicatorData";
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
  keyLevelsEnabled?: boolean;
  keyLevelsCount?: number;
  onToggleKeyLevels?: () => void;
  layersCount?: number;
  onOpenLayers?: () => void;
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
  keyLevelsEnabled = false,
  keyLevelsCount = 0,
  onToggleKeyLevels,
  layersCount = 0,
  onOpenLayers,
}: IndicatorToolbarProps) {
  const locale = useLocale();
  const [editing, setEditing] = useState<IndicatorName | null>(null);
  const seriesMap = new Map<IndicatorName, IndicatorSeries>(
    (bundle?.series ?? []).map((item) => [item.name, item]),
  );

  return (
    <div className="indicator-bar" data-testid="indicator-toolbar">
      <div className="indicator-chips">
        {INDICATOR_ORDER.map((name) => {
          const item = settings[name];
          const series = seriesMap.get(name);
          const color = INDICATOR_COLORS[name];
          return (
            <div key={name} className={`indicator-chip${item.enabled ? " is-enabled" : ""}`}>
              <button
                type="button"
                className="indicator-toggle"
                data-testid={`indicator-toggle-${name}`}
                aria-pressed={item.enabled}
                onClick={() => onChange(name, { enabled: !item.enabled })}
              >
                <span className="indicator-swatch" style={{ background: color }} />
                <span>{series ? describeParams(series, locale) : indicatorLabel(name, locale)}</span>
                {series && (
                  <span className="indicator-latest">{describeLatest(series)}</span>
                )}
              </button>
              <button
                type="button"
                className="indicator-gear"
                aria-label={t(`配置 ${name}`, `Configure ${name}`)}
                onClick={() => setEditing(editing === name ? null : name)}
              >
                ⚙
              </button>
            </div>
          );
        })}
        {onToggleKeyLevels && (
          <div className={`indicator-chip${keyLevelsEnabled ? " is-enabled" : ""}`}>
            <button
              type="button"
              className="indicator-toggle"
              data-testid="indicator-toggle-KEY_LEVELS"
              aria-pressed={keyLevelsEnabled}
              onClick={onToggleKeyLevels}
            >
              <span className="indicator-swatch" style={{ background: "#4c8dff" }} />
              <span>{t("关键位", "Key Levels")}</span>
              {keyLevelsEnabled && keyLevelsCount > 0 && (
                <span className="indicator-latest">{keyLevelsCount}</span>
              )}
            </button>
          </div>
        )}
        {onOpenLayers && (
          <div className="indicator-chip">
            <button
              type="button"
              className="indicator-toggle"
              data-testid="chart-open-layers"
              onClick={onOpenLayers}
            >
              <Layers size={12} />
              <span>{t("图层", "Layers")}</span>
              {layersCount > 0 && <span className="indicator-latest">{layersCount}</span>}
            </button>
          </div>
        )}
        {loading && (
          <span className="indicator-status">{t("计算中…", "Computing…")}</span>
        )}
        {error && (
          <span className="indicator-status indicator-status--error" role="status">
            {error}
          </span>
        )}
      </div>

      {editing !== null && (
        <div className="indicator-popover" data-testid={`indicator-editor-${editing}`}>
          <strong>{indicatorLabel(editing, locale)}</strong>
          <label>
            <span>{t("周期", "Period")}</span>
            <input
              type="number"
              min={1}
              max={5000}
              value={settings[editing].period}
              onChange={(e) =>
                onChange(editing, {
                  period: Math.max(1, Math.min(5000, Number(e.target.value) || 1)),
                })
              }
            />
          </label>
          {editing === "BOLL" && (
            <label>
              <span>{t("标准差 σ", "Std dev σ")}</span>
              <input
                type="text"
                value={settings[editing].stdDev}
                onChange={(e) => onChange(editing, { stdDev: e.target.value })}
              />
            </label>
          )}
          {editing === "VWAP" && (
            <label>
              <span>{t("锚点", "Anchor")}</span>
              <select
                value={settings[editing].anchor}
                onChange={(e) =>
                  onChange(editing, {
                    anchor: e.target.value === "session" ? "session" : "snapshot",
                  })
                }
              >
                <option value="session">{anchorLabel("session", locale)}</option>
                <option value="snapshot">{anchorLabel("snapshot", locale)}</option>
              </select>
            </label>
          )}
          <div className="indicator-popover-actions">
            <button
              type="button"
              className="ghost-button"
              disabled={savingDefault === editing}
              onClick={() => onSaveDefault(editing)}
            >
              {t("设为默认", "Save default")}
            </button>
            {!isDefaultParams(editing, settings[editing], defaults) && (
              <button
                type="button"
                className="ghost-button"
                disabled={savingDefault === editing}
                onClick={() => onResetDefault(editing)}
              >
                {t("恢复默认", "Reset")}
              </button>
            )}
            <button
              type="button"
              className="ghost-button"
              onClick={() => setEditing(null)}
            >
              {t("完成", "Done")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
