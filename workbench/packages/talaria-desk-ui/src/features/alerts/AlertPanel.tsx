import { useEffect, useMemo, useState, type FormEvent } from "react";
import type {
  AlertDirection,
  AlertLevelEvent,
  AlertMetric,
  AlertOverlayMode,
  AlertTriggerRecord,
  DrawingRecord,
  IndicatorName,
  InstrumentSpec,
  KeyLevelKind,
} from "@talaria/contracts";

import { t, useLocale } from "../../lib/i18n";
import { TIMEFRAMES } from "../../workbench/panels";
import {
  ALERT_INDICATORS,
  ALERT_LEVEL_EVENTS,
  ALERT_OVERLAY_KINDS,
  ALERT_TABS,
  alertStatusLabel,
  alertTabLabel,
  alertsForTab,
  buildAlertCreate,
  conditionSummary,
  defaultAlertForm,
  drawingKindFor,
  formatCooldown,
  metricLabels,
  relativeTime,
  statusToTab,
  tabCounts,
  triggerSummary,
  validateAlertForm,
  type AlertFormKind,
  type AlertFormState,
  type AlertTabId,
} from "./model";
import type { AlertsController } from "./useAlerts";

export interface AlertFormSeed {
  nonce: number;
  drawingId?: string;
  price?: number | null;
  levelId?: string;
  levelKind?: KeyLevelKind | null;
  instrumentId?: string;
  timeframe?: string;
}

export interface AlertPanelProps {
  deniedAlerts?: string[];
  controller: AlertsController;
  instruments: InstrumentSpec[];
  drawings: DrawingRecord[];
  currentInstrument: string;
  currentTimeframe: string;
  formSeed: AlertFormSeed | null;
  replayActive?: boolean;
  onLocate: (trigger: AlertTriggerRecord) => void;
}

export function AlertPanel({
  deniedAlerts = [],
  controller,
  instruments,
  drawings,
  currentInstrument,
  currentTimeframe,
  formSeed,
  replayActive = false,
  onLocate,
}: AlertPanelProps) {
  useLocale();
  const [tab, setTab] = useState<AlertTabId>("active");
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState<AlertFormState>(() =>
    defaultAlertForm(currentInstrument, currentTimeframe),
  );
  const [formError, setFormError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (formSeed === null) return;
    const hasRemoteDrawing =
      formSeed.drawingId !== undefined &&
      drawings.some((d) => d.drawing_id === formSeed.drawingId);
    setForm(
      defaultAlertForm(
        formSeed.instrumentId ?? currentInstrument,
        formSeed.timeframe ?? currentTimeframe,
        {
          drawingId: hasRemoteDrawing ? formSeed.drawingId : undefined,
          price: formSeed.price ?? null,
          levelId: formSeed.levelId,
          levelKind: formSeed.levelKind,
        },
      ),
    );
    setFormError(null);
    setFormOpen(true);
    setTab("active");
  }, [formSeed, currentInstrument, currentTimeframe, drawings]);

  const counts = useMemo(() => tabCounts(controller.alerts), [controller.alerts]);
  const visibleAlerts = useMemo(
    () => alertsForTab(controller.alerts, tab),
    [controller.alerts, tab],
  );
  const selectableDrawings = useMemo(
    () =>
      drawings.filter(
        (d) =>
          d.instrument_id === form.instrumentId &&
          ALERT_OVERLAY_KINDS.includes(d.kind),
      ),
    [drawings, form.instrumentId],
  );

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const validation = validateAlertForm(form);
    if (validation !== null) {
      setFormError(validation);
      return;
    }
    const created = await controller.create(buildAlertCreate(form));
    if (!created) {
      setFormError(controller.error ?? t("保存预警失败", "Failed to save alert"));
      return;
    }
    setFormOpen(false);
    setTab(statusToTab(created.status));
  };

  return (
    <section
      className="dock-panel alerts-panel"
      data-testid="alerts-panel"
      aria-label={t("预警工作台", "Alert workbench")}
    >
      <header className="dock-panel-head">
        <div>
          <h2>
            {t("预警", "Alerts")}{" "}
            <span className="alerts-live-badge" data-testid="alerts-live-badge">
              {t("实时", "LIVE")}
            </span>
          </h2>
          <span className="dock-panel-meta">
            {t("阈值 · 指标 · 画线 · 关键位", "Threshold · Indicator · Drawing · Level")}
          </span>
        </div>
        <button
          type="button"
          className="primary-button"
          data-testid="alert-create-open"
          onClick={() => {
            setForm(defaultAlertForm(currentInstrument, currentTimeframe));
            setFormError(null);
            setFormOpen((v) => !v);
          }}
        >
          {formOpen ? t("取消", "Cancel") : t("+ 新建预警", "+ New alert")}
        </button>
      </header>

      {replayActive && (
        <p className="panel-note" data-testid="alerts-replay-isolation-note" role="status">
          {t(
            "实时预警按实时行情触发，回放期间不叠加到回放图表。",
            "Live alerts run on real-time market data and do not overlay onto replay charts.",
          )}
        </p>
      )}

      {deniedAlerts.length > 0 && (
        <p className="panel-note" role="status">
          {t(
            `${deniedAlerts.length} 条预警因实时订阅配额暂缓。`,
            `${deniedAlerts.length} alerts waiting for live quote quota.`,
          )}
        </p>
      )}

      {formOpen && (
        <form className="alert-form" data-testid="alert-form" onSubmit={submit}>
          <div className="form-row">
            <select
              value={form.instrumentId}
              onChange={(e) => setForm({ ...form, instrumentId: e.target.value })}
            >
              {instruments.map((inst) => (
                <option key={inst.instrument_id} value={inst.instrument_id}>
                  {inst.instrument_id}
                </option>
              ))}
            </select>
            <select
              value={form.timeframe}
              onChange={(e) => setForm({ ...form, timeframe: e.target.value })}
            >
              {TIMEFRAMES.map((tf) => (
                <option key={tf} value={tf}>
                  {tf}
                </option>
              ))}
            </select>
            <select
              value={form.kind}
              onChange={(e) =>
                setForm({
                  ...form,
                  kind: e.target.value as AlertFormKind,
                  drawingId:
                    e.target.value === "overlay" && !form.drawingId
                      ? (selectableDrawings[0]?.drawing_id ?? "")
                      : form.drawingId,
                })
              }
            >
              <option value="metric">{t("价格/衍生品", "Metric")}</option>
              <option value="indicator">{t("指标阈值", "Indicator")}</option>
              <option value="overlay">{t("画线突破", "Drawing cross")}</option>
              <option value="level">{t("关键位事件", "Key level")}</option>
            </select>
          </div>

          {form.kind === "metric" && (
            <div className="form-row">
              <select
                value={form.metric}
                onChange={(e) =>
                  setForm({ ...form, metric: e.target.value as AlertMetric })
                }
              >
                {Object.entries(metricLabels()).map(([val, label]) => (
                  <option key={val} value={val}>
                    {label}
                  </option>
                ))}
              </select>
              <select
                value={form.direction}
                onChange={(e) =>
                  setForm({ ...form, direction: e.target.value as AlertDirection })
                }
              >
                <option value="ABOVE">{t("上穿/高于", "Above ≥")}</option>
                <option value="BELOW">{t("下穿/低于", "Below ≤")}</option>
              </select>
              <input
                type="text"
                placeholder={t("目标价格", "Target price")}
                value={form.threshold}
                onChange={(e) => setForm({ ...form, threshold: e.target.value })}
              />
            </div>
          )}

          {form.kind === "indicator" && (
            <div className="form-row">
              <select
                value={form.indicator}
                onChange={(e) =>
                  setForm({ ...form, indicator: e.target.value as IndicatorName })
                }
              >
                {ALERT_INDICATORS.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
              <select
                value={form.direction}
                onChange={(e) =>
                  setForm({ ...form, direction: e.target.value as AlertDirection })
                }
              >
                <option value="ABOVE">≥</option>
                <option value="BELOW">≤</option>
              </select>
              <input
                type="text"
                placeholder={t("阈值", "Threshold")}
                value={form.threshold}
                onChange={(e) => setForm({ ...form, threshold: e.target.value })}
              />
            </div>
          )}

          {form.kind === "overlay" && (
            <div className="form-row">
              <select
                value={form.drawingId}
                onChange={(e) => setForm({ ...form, drawingId: e.target.value })}
              >
                <option value="">{t("选择画线…", "Select drawing…")}</option>
                {selectableDrawings.map((d) => (
                  <option key={d.drawing_id} value={d.drawing_id}>
                    {d.drawing_id.slice(0, 8)} · {d.kind}
                  </option>
                ))}
              </select>
              <select
                value={form.overlayMode}
                onChange={(e) =>
                  setForm({
                    ...form,
                    overlayMode: e.target.value as AlertOverlayMode,
                  })
                }
              >
                <option value="CLOSE_BREAK">{t("收盘突破", "Close break")}</option>
                <option value="TOUCH">{t("触碰", "Touch")}</option>
              </select>
            </div>
          )}

          {form.kind === "level" && (
            <div className="form-row">
              <input
                type="text"
                placeholder="level_id"
                value={form.levelId}
                onChange={(e) => setForm({ ...form, levelId: e.target.value })}
              />
              <select
                value={form.levelEvent}
                onChange={(e) =>
                  setForm({
                    ...form,
                    levelEvent: e.target.value as AlertLevelEvent,
                  })
                }
              >
                {ALERT_LEVEL_EVENTS.map((ev) => (
                  <option key={ev} value={ev}>
                    {ev}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div className="form-row">
            <input
              type="text"
              placeholder={t("备注（可选）", "Note (optional)")}
              value={form.note}
              onChange={(e) => setForm({ ...form, note: e.target.value })}
            />
            <button type="submit" className="primary-button">
              {t("保存预警", "Save alert")}
            </button>
          </div>
          {formError && (
            <p className="panel-error" role="alert">
              {formError}
            </p>
          )}
        </form>
      )}

      <div className="tab-strip" role="tablist">
        {ALERT_TABS.map((tabId) => (
          <button
            key={tabId}
            type="button"
            role="tab"
            aria-selected={tab === tabId}
            className={`tab-button${tab === tabId ? " is-active" : ""}`}
            onClick={() => setTab(tabId)}
          >
            {alertTabLabel(tabId)} ({counts[tabId]})
          </button>
        ))}
      </div>

      {visibleAlerts.length === 0 ? (
        <p className="panel-empty">{t("当前标签无预警规则。", "No alerts in this tab.")}</p>
      ) : (
        <ul className="entity-list">
          {visibleAlerts.map((alert) => {
            const drawingKind =
              alert.condition.expression.op === "overlay"
                ? drawingKindFor(alert.condition.expression.drawing_id, drawings)
                : null;
            return (
              <li key={alert.alert_id} className="entity-card">
                <div className="entity-card-top">
                  <span className="entity-tag">
                    {alert.instrument_id} · {alert.timeframe}
                  </span>
                  <span className={`status-pill status-${alert.status.toLowerCase()}`}>
                    {alertStatusLabel(alert.status)}
                  </span>
                </div>
                <div className="entity-card-main">
                  <strong>{conditionSummary(alert.condition, drawingKind)}</strong>
                  <span>
                    {formatCooldown(alert.cooldown_ms)} · {relativeTime(alert.updated_ts, now)}
                  </span>
                </div>
                <div className="entity-actions">
                  {alert.status === "DRAFT" && (
                    <button
                      type="button"
                      className="ghost-button"
                      onClick={() => void controller.confirm(alert.alert_id)}
                    >
                      {t("启用", "Arm")}
                    </button>
                  )}
                  <button
                    type="button"
                    className="ghost-button"
                    onClick={() =>
                      void controller.update(alert.alert_id, {
                        enabled: alert.status === "DISABLED",
                      })
                    }
                  >
                    {alert.status === "DISABLED" ? t("启用", "Enable") : t("停用", "Disable")}
                  </button>
                  <button
                    type="button"
                    className="ghost-button"
                    onClick={() => void controller.remove(alert.alert_id)}
                  >
                    {t("删除", "Delete")}
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {controller.triggers.length > 0 && (
        <div className="alert-triggers">
          <h3>{t("触发记录", "Recent triggers")}</h3>
          <ul className="entity-list">
            {controller.triggers.slice(0, 10).map((trig) => (
              <li key={trig.trigger_id} className="entity-card">
                <button
                  type="button"
                  className="drawing-select-button"
                  onClick={() => onLocate(trig)}
                >
                  <strong>{triggerSummary(trig)}</strong>
                  <span>{relativeTime(trig.triggered_ts, now)}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

export function AlertToast({
  trigger,
  onLocate,
  onDismiss,
}: {
  trigger: AlertTriggerRecord;
  onLocate: (trigger: AlertTriggerRecord) => void;
  onDismiss: () => void;
}) {
  useLocale();
  return (
    <div className="alert-toast" role="status" data-testid="alert-toast">
      <div>
        <strong>{t("预警触发", "Alert triggered")}</strong>
        <p>{triggerSummary(trigger)}</p>
      </div>
      <div className="alert-toast-actions">
        <button type="button" className="primary-button" onClick={() => onLocate(trigger)}>
          {t("定位", "Locate")}
        </button>
        <button type="button" className="ghost-button" onClick={onDismiss}>
          ×
        </button>
      </div>
    </div>
  );
}
