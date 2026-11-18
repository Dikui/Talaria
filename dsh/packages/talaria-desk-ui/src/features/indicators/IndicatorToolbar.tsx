import { useState } from "react";

import type { IndicatorBundle, IndicatorName, IndicatorSeries } from "@talaria/contracts";

import {
  INDICATOR_COLORS,
  INDICATOR_LABELS,
  INDICATOR_ORDER,
  anchorLabel,
  type IndicatorSetting,
  type IndicatorSettings,
} from "./config";
import { describeLatest, describeParams } from "../../lib/indicatorData";

interface IndicatorToolbarProps {
  settings: IndicatorSettings;
  onChange: (name: IndicatorName, patch: Partial<IndicatorSetting>) => void;
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

function LegendChip({ series }: { series: IndicatorSeries }) {
  return (
    <span className="indicator-chip" title={`${describeParams(series)} ${describeLatest(series)}`}>
      <span
        className="indicator-dot"
        style={{ background: INDICATOR_COLORS[series.name] }}
        aria-hidden="true"
      />
      <strong>{INDICATOR_LABELS[series.name] ?? series.name}</strong>
      <span className="indicator-params">{describeParams(series)}</span>
      <span className="indicator-latest" data-testid={`indicator-latest-${series.name}`}>
        {describeLatest(series)}
      </span>
    </span>
  );
}

export function IndicatorToolbar({
  settings,
  onChange,
  bundle,
  loading,
  error,
}: IndicatorToolbarProps) {
  const [collapsed, setCollapsed] = useState(false);
  const series = bundle?.series ?? [];

  return (
    <div className={`indicator-bar${collapsed ? " is-collapsed" : ""}`}>
      <div className="indicator-legend" data-testid="indicator-legend">
        {series.length === 0 ? (
          <span className="indicator-empty">
            {loading ? "指标计算中…" : "未启用指标"}
          </span>
        ) : (
          series.map((item) => <LegendChip key={item.name} series={item} />)
        )}
        {loading && series.length > 0 && (
          <span className="indicator-loading" aria-live="polite">
            更新中…
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
            {collapsed ? "展开参数" : "折叠参数"}
          </button>
        )}
      </div>

      <details className="indicator-settings" data-testid="indicator-settings">
        <summary>指标设置</summary>
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
                    aria-label={`启用 ${name}`}
                  />
                  <span
                    className="indicator-dot"
                    style={{ background: INDICATOR_COLORS[name] }}
                    aria-hidden="true"
                  />
                  <strong>{INDICATOR_LABELS[name] ?? name}</strong>
                </label>
                {name === "VWAP" ? (
                  <label className="indicator-param">
                    <span>锚点</span>
                    <select
                      value={setting.anchor}
                      disabled={!setting.enabled}
                      onChange={(event) =>
                        onChange(name, {
                          anchor: event.target.value as "snapshot" | "session",
                        })
                      }
                    >
                      <option value="snapshot">{anchorLabel("snapshot")}</option>
                      <option value="session">{anchorLabel("session")}</option>
                    </select>
                  </label>
                ) : (
                  <label className="indicator-param">
                    <span>周期</span>
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
                    <span>倍数</span>
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
              </div>
            );
          })}
        </div>
      </details>
    </div>
  );
}
