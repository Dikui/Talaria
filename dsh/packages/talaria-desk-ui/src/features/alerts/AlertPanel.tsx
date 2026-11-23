import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type {
  AlertDirection,
  AlertMetric,
  AlertOverlayMode,
  AlertRecord,
  AlertTriggerRecord,
  DrawingRecord,
  IndicatorName,
  InstrumentSpec,
  KeyLevelKind,
} from "@talaria/contracts";

import { t, useLocale } from "../../lib/i18n";
import type { Theme } from "../../lib/theme";
import {
  ALERT_INDICATORS,
  ALERT_LEVEL_EVENTS,
  ALERT_OVERLAY_KINDS,
  ALERT_TABS,
  alertLevelEventLabel,
  alertStatusLabel,
  alertTabLabel,
  alertsForTab,
  buildAlertCreate,
  conditionSummary,
  defaultAlertForm,
  drawingKindFor,
  drawingKindLabel,
  formatCooldown,
  formFromRecord,
  indicatorNameLabel,
  metricLabels,
  relativeTime,
  statusToTab,
  tabCounts,
  timeUntil,
  triggerSummary,
  validateAlertForm,
  type AlertFormState,
  type AlertFormKind,
  type AlertTabId,
} from "./model";
import type { AlertsController } from "./useAlerts";

const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"];

export interface AlertFormSeed {
  nonce: number;
  drawingId?: string;
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
  onLocate: (trigger: AlertTriggerRecord) => void;
  onClose?: () => void;
  tabBar?: ReactNode;
}

export function AlertPanel({
  deniedAlerts = [],
  controller,
  instruments,
  drawings,
  currentInstrument,
  currentTimeframe,
  formSeed,
  onLocate,
  onClose,
  tabBar,
}: AlertPanelProps) {
  useLocale();
  const [tab, setTab] = useState<AlertTabId>("active");
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState<AlertFormState>(() =>
    defaultAlertForm(currentInstrument, currentTimeframe),
  );
  const [formError, setFormError] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [showHistory, setShowHistory] = useState(true);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (formSeed === null) {
      return;
    }
    setForm(
      defaultAlertForm(
        formSeed.instrumentId ?? currentInstrument,
        formSeed.timeframe ?? currentTimeframe,
        {
          drawingId: formSeed.drawingId,
          levelId: formSeed.levelId,
          levelKind: formSeed.levelKind,
        },
      ),
    );
    setEditingId(null);
    setFormError(null);
    setFormOpen(true);
    setTab("active");
  }, [formSeed, currentInstrument, currentTimeframe]);

  const counts = useMemo(() => tabCounts(controller.alerts), [controller.alerts]);
  const visibleAlerts = useMemo(
    () => alertsForTab(controller.alerts, tab),
    [controller.alerts, tab],
  );
  const selectableDrawings = useMemo(
    () =>
      drawings.filter(
        (drawing) =>
          drawing.instrument_id === form.instrumentId &&
          ALERT_OVERLAY_KINDS.includes(drawing.kind),
      ),
    [drawings, form.instrumentId],
  );

  const beginCreate = () => {
    setForm(defaultAlertForm(currentInstrument, currentTimeframe));
    setEditingId(null);
    setFormError(null);
    setFormOpen(true);
  };

  const beginEdit = (alert: AlertRecord) => {
    setForm(formFromRecord(alert));
    setEditingId(alert.alert_id);
    setFormError(null);
    setFormOpen(true);
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const validation = validateAlertForm(form);
    if (validation !== null) {
      setFormError(validation);
      return;
    }
    const payload = buildAlertCreate(form);
    const result =
      editingId === null
        ? await controller.create(payload)
        : await controller.update(editingId, {
            ...(form.savedRule ? {} : { condition: payload.condition }),
            note: payload.note,
            repeat: payload.repeat,
            cooldown_ms: payload.cooldown_ms,
          });
    if (result === null) {
      setFormError(controller.error ?? t("保存预警失败", "Failed to save alert"));
      return;
    }
    setFormOpen(false);
    setEditingId(null);
    setTab(statusToTab(result.status));
  };

  const changeKind = (kind: AlertFormKind) => {
    setForm((current) => ({
      ...current,
      kind,
      drawingId:
        kind === "overlay" && current.drawingId === ""
          ? (selectableDrawings[0]?.drawing_id ?? "")
          : current.drawingId,
      levelKind: kind === "level" ? current.levelKind : null,
    }));
  };

  return (
    <aside className="workspace-copilot alerts-panel" data-testid="alerts-panel">
      {tabBar}
      <div className="alerts-head">
        <div>
          <h2 className="alerts-title">{t("预警", "Alerts")}</h2>
          <p className="alerts-subtitle">{t("阈值 · 指标 · 画线 · 关键位", "Threshold · Indicator · Drawing · Key level")}</p>
        </div>
        <div className="alerts-head-actions">
          <button
            type="button"
            className="ghost-button alerts-sound"
            aria-pressed={controller.soundEnabled}
            onClick={() => controller.setSoundEnabled(!controller.soundEnabled)}
            title={t("触发提示音", "Alert sound")}
          >
            {controller.soundEnabled ? t("声音开", "Sound on") : t("声音关", "Sound off")}
          </button>
          {onClose !== undefined && (
            <button
              type="button"
              className="ghost-button"
              aria-label={t("收起预警面板", "Collapse alerts panel")}
              onClick={onClose}
            >
              ×
            </button>
          )}
        </div>
      </div>

      <div className="alerts-create-row">
        <button
          type="button"
          className="primary-button alerts-create"
          onClick={beginCreate}
        >
          {t("新建预警", "New alert")}
        </button>
      </div>

      {controller.error !== null && (
        <div className="alerts-error" role="alert" data-testid="alerts-error">
          <span>{controller.error}</span>
          <button type="button" aria-label={t("关闭预警错误", "Dismiss alert error")} onClick={controller.clearError}>
            ×
          </button>
        </div>
      )}

      {formOpen && (
        <form className="alert-form" onSubmit={submit} data-testid="alert-form">
          <div className="alert-form-title">{editingId === null ? t("新建预警", "New alert") : t("编辑预警", "Edit alert")}</div>
          <label>
            {t("标的", "Instrument")}
            <select
              value={form.instrumentId}
              onChange={(event) =>
                setForm((current) => ({ ...current, instrumentId: event.target.value }))
              }
            >
              {instruments.map((instrument) => (
                <option key={instrument.instrument_id} value={instrument.instrument_id}>
                  {instrument.instrument_id}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t("周期", "Timeframe")}
            <select
              value={form.timeframe}
              onChange={(event) =>
                setForm((current) => ({ ...current, timeframe: event.target.value }))
              }
            >
              {TIMEFRAMES.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
          {form.savedRule ? (
            <p className="alert-condition">{conditionSummary(form.savedRule)}</p>
          ) : (
            <label>
              {t("条件", "Condition")}
              <select
                value={form.kind}
                onChange={(event) => changeKind(event.target.value as AlertFormKind)}
              >
                <option value="metric">{t("指标阈值", "Indicator threshold")}</option>
                <option value="indicator">{t("指标信号", "Indicator signal")}</option>
                <option value="overlay">{t("画线触碰", "Drawing touch")}</option>
                <option value="level">{t("关键位事件", "Key level event")}</option>
              </select>
            </label>
          )}

          {form.kind === "metric" && (
            <>
              <label>
                {t("指标", "Metric")}
                <select
                  value={form.metric}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      metric: event.target.value as AlertMetric,
                    }))
                  }
                >
                  {Object.entries(metricLabels()).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {t("阈值", "Threshold")}
                <input
                  value={form.threshold}
                  inputMode="decimal"
                  placeholder={t("例如 42000.5", "e.g. 42000.5")}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, threshold: event.target.value }))
                  }
                />
              </label>
            </>
          )}

          {form.kind === "indicator" && (
            <>
              <label>
                {t("指标", "Indicator")}
                <select
                  value={form.indicator}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      indicator: event.target.value as IndicatorName,
                    }))
                  }
                >
                  {ALERT_INDICATORS.map((name) => (
                    <option key={name} value={name}>
                      {indicatorNameLabel(name)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {t("周期", "Period")}
                <input
                  type="number"
                  min={0}
                  max={5000}
                  placeholder={t("0 = 服务端默认", "0 = server default")}
                  value={form.indicatorPeriod}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      indicatorPeriod: Number(event.target.value),
                    }))
                  }
                />
              </label>
              {form.indicator === "VWAP" && (
                <label>
                  {t("VWAP 锚点", "VWAP anchor")}
                  <select
                    value={form.indicatorAnchor}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        indicatorAnchor: event.target.value as AlertFormState["indicatorAnchor"],
                      }))
                    }
                  >
                    <option value="">{t("使用默认值", "Use default")}</option>
                    <option value="session">{t("UTC 日界", "UTC day boundary")}</option>
                    <option value="snapshot">{t("快照起点", "Snapshot start")}</option>
                  </select>
                </label>
              )}
              <label>
                {t("对比指标", "Compare with")}
                <select
                  value={form.compareIndicator}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      compareIndicator: event.target.value as IndicatorName | "",
                    }))
                  }
                >
                  <option value="">{t("无（用阈值）", "None (use threshold)")}</option>
                  {ALERT_INDICATORS.map((name) => (
                    <option key={name} value={name}>
                      {indicatorNameLabel(name)}
                    </option>
                  ))}
                </select>
              </label>
              {form.compareIndicator === "" ? (
                <label>
                  {t("阈值", "Threshold")}
                  <input
                    value={form.threshold}
                    inputMode="decimal"
                    placeholder={t("例如 70", "e.g. 70")}
                    onChange={(event) =>
                      setForm((current) => ({ ...current, threshold: event.target.value }))
                    }
                  />
                </label>
              ) : (
                <label>
                  {t("对比周期", "Comparison period")}
                  <input
                    type="number"
                    min={0}
                    max={5000}
                    placeholder={t("0 = 服务端默认", "0 = server default")}
                    value={form.comparePeriod}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        comparePeriod: Number(event.target.value),
                      }))
                    }
                  />
                </label>
              )}
              {form.compareIndicator === "VWAP" && (
                <label>
                  {t("对比 VWAP 锚点", "Comparison VWAP anchor")}
                  <select
                    value={form.compareAnchor}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        compareAnchor: event.target.value as AlertFormState["compareAnchor"],
                      }))
                    }
                  >
                    <option value="">{t("使用默认值", "Use default")}</option>
                    <option value="session">{t("UTC 日界", "UTC day boundary")}</option>
                    <option value="snapshot">{t("快照起点", "Snapshot start")}</option>
                  </select>
                </label>
              )}
            </>
          )}

          {form.kind === "overlay" && (
            <>
              <label>
                {t("画线", "Drawing")}
                <select
                  value={form.drawingId}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, drawingId: event.target.value }))
                  }
                >
                  <option value="">{t("选择画线", "Select a drawing")}</option>
                  {selectableDrawings.map((drawing) => (
                    <option key={drawing.drawing_id} value={drawing.drawing_id}>
                      {drawingKindLabel(drawing.kind)} · {drawing.drawing_id.slice(4, 10)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {t("触发方式", "Trigger mode")}
                <select
                  value={form.overlayMode}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      overlayMode: event.target.value as AlertOverlayMode,
                    }))
                  }
                >
                  <option value="CLOSE_BREAK">{t("收盘突破", "Close break")}</option>
                  <option value="TOUCH">{t("盘中触碰", "Intraday touch")}</option>
                </select>
              </label>
            </>
          )}

          {form.kind === "level" && (
            <>
              <label>
                {t("关键位 level_id", "Key level level_id")}
                <input
                  value={form.levelId}
                  placeholder="kl_0123456789abcdef"
                  aria-label={t("关键位 level_id", "Key level level_id")}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      levelId: event.target.value,
                    }))
                  }
                />
              </label>
              <label>
                {t("关键位事件", "Key level event")}
                <select
                  value={form.levelEvent}
                  aria-label={t("关键位事件", "Key level event")}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      levelEvent: event.target
                        .value as AlertFormState["levelEvent"],
                    }))
                  }
                >
                  {ALERT_LEVEL_EVENTS.map((event) => (
                    <option key={event} value={event}>
                      {alertLevelEventLabel(event)}
                    </option>
                  ))}
                </select>
              </label>
              {form.levelKind !== null && (
                <p className="alert-form-hint" data-testid="alert-level-kind">
                  {t("类型：", "Type: ")}{form.levelKind}
                </p>
              )}
            </>
          )}

          {form.kind !== "level" && form.kind !== "rule" && (
          <label>
            {t("方向", "Direction")}
            <select
              value={form.direction}
              onChange={(event) =>
                setForm((current) => ({
                  ...current,
                  direction: event.target.value as AlertDirection,
                }))
              }
            >
              <option value="ABOVE">{t("上穿 / 高于", "Above / cross above")}</option>
              <option value="BELOW">{t("下穿 / 低于", "Below / cross below")}</option>
            </select>
          </label>
          )}
          <label>
            {t("冷却（秒）", "Cooldown (s)")}
            <input
              type="number"
              min={0}
              value={form.cooldownSeconds}
              onChange={(event) =>
                setForm((current) => ({
                  ...current,
                  cooldownSeconds: Number(event.target.value),
                }))
              }
            />
          </label>
          <label>
            {t("备注", "Note")}
            <textarea
              value={form.note}
              maxLength={500}
              rows={2}
              placeholder={t("可选，最多 500 字", "Optional, up to 500 characters")}
              onChange={(event) =>
                setForm((current) => ({ ...current, note: event.target.value }))
              }
            />
          </label>
          <div className="alert-form-checks">
            <label className="alert-check">
              <input
                type="checkbox"
                checked={form.repeat}
                onChange={(event) =>
                  setForm((current) => ({ ...current, repeat: event.target.checked }))
                }
              />
              {t("重复提醒", "Repeat alert")}
            </label>
            <label className="alert-check">
              <input
                type="checkbox"
                checked={form.draft}
                disabled={editingId !== null}
                onChange={(event) =>
                  setForm((current) => ({ ...current, draft: event.target.checked }))
                }
              />
              {t("保存为草案", "Save as draft")}
            </label>
          </div>
          {formError !== null && (
            <div className="alert-form-error" role="alert">
              {formError}
            </div>
          )}
          <div className="alert-form-actions">
            <button type="submit" className="primary-button">
              {t("保存", "Save")}
            </button>
            <button
              type="button"
              className="ghost-button"
              onClick={() => {
                setFormOpen(false);
                setEditingId(null);
                setFormError(null);
              }}
            >
              {t("取消", "Cancel")}
            </button>
          </div>
        </form>
      )}

      <div className="alerts-tabs" role="tablist" aria-label={t("预警规则分类", "Alert rule categories")}>
        {ALERT_TABS.map((id) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            className={`alerts-tab${tab === id ? " is-active" : ""}`}
            onClick={() => setTab(id)}
          >
            {alertTabLabel(id)}
            <span className="alerts-tab-count">{counts[id]}</span>
          </button>
        ))}
      </div>

      <div className="alerts-list" data-testid="alerts-list">
        {controller.loading ? (
          <p className="alerts-empty">{t("正在加载预警…", "Loading alerts…")}</p>
        ) : visibleAlerts.length === 0 ? (
          <div className="alerts-empty" data-testid="alerts-empty">
            <strong>{t(`${alertTabLabel(tab)}暂无规则`, `${alertTabLabel(tab)} — no rules`)}</strong>
            <span>{t("新建预警，或从画线属性一键挂载。", "Create an alert, or attach one from drawing properties.")}</span>
          </div>
        ) : (
          visibleAlerts.map((alert) => (
            <AlertCard
              key={alert.alert_id}
              alert={alert}
              quotaDenied={deniedAlerts.includes(alert.alert_id)}
              drawings={drawings}
              now={now}
              controller={controller}
              onEdit={() => beginEdit(alert)}
            />
          ))
        )}
      </div>

      <div className="alerts-history">
        <button
          type="button"
          className="alerts-history-toggle"
          aria-expanded={showHistory}
          onClick={() => setShowHistory((open) => !open)}
        >
          {t("触发历史", "Trigger history")} {controller.triggers.length}
          <span>{showHistory ? t("收起", "Collapse") : t("展开", "Expand")}</span>
        </button>
        {showHistory &&
          (controller.triggers.length === 0 ? (
            <p className="alerts-empty" data-testid="triggers-empty">
              {t("暂无触发记录。", "No triggers yet.")}
            </p>
          ) : (
            <ul className="trigger-list" data-testid="trigger-list">
              {controller.triggers.map((trigger) => (
                <li key={trigger.trigger_id} className="trigger-item">
                  <button
                    type="button"
                    className="trigger-main"
                    onClick={() => onLocate(trigger)}
                    title={t("定位到触发时刻", "Go to trigger time")}
                  >
                    <span className="trigger-message">{triggerSummary(trigger)}</span>
                    <span className="trigger-meta">
                      {trigger.instrument_id} · {trigger.timeframe} ·{" "}
                      {relativeTime(trigger.triggered_ts, now)}
                      {trigger.snapshot_id !== "" ? t(" · 有快照", " · snapshot") : ""}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ))}
      </div>
    </aside>
  );
}

interface AlertCardProps {
  quotaDenied: boolean;
  alert: AlertRecord;
  drawings: DrawingRecord[];
  now: number;
  controller: AlertsController;
  onEdit: () => void;
}

function AlertCard({ quotaDenied, alert, drawings, now, controller, onEdit }: AlertCardProps) {
  useLocale();
  const expression = alert.condition.expression;
  const kind = expression.op === "overlay" ? drawingKindFor(expression.drawing_id, drawings) : null;
  return (
    <article
      className={`alert-item alert-item--${alert.status.toLowerCase()}${
        controller.toast?.alert_id === alert.alert_id ? " alert-item--recent" : ""
      }`}
    >
      <header className="alert-item-head">
        <span className={`alert-status alert-status--${alert.status.toLowerCase()}`}>
          {quotaDenied ? t("配额不足、未在监听", "Quota exceeded, not listening") : alertStatusLabel(alert.status)}
        </span>
        <span className="alert-instrument">
          {alert.instrument_id} · {alert.timeframe}
        </span>
        <span className="alert-updated">{relativeTime(alert.updated_ts, now)}</span>
      </header>
      <p className="alert-condition">{conditionSummary(alert.condition, kind)}</p>
      {alert.note !== undefined && alert.note !== "" && (
        <p className="alert-note">{alert.note}</p>
      )}
      <p className="alert-meta">
        {alert.repeat
          ? t(`重复 · 冷却 ${formatCooldown(alert.cooldown_ms)}`, `Repeating · cooldown ${formatCooldown(alert.cooldown_ms)}`)
          : t("单次触发", "Single trigger")}
        {alert.next_arm_ts !== undefined && alert.next_arm_ts !== null
          ? t(
              ` · ${timeUntil(alert.next_arm_ts, now)}回到待命`,
              ` · re-arms ${timeUntil(alert.next_arm_ts, now)}`,
            )
          : ""}
        {alert.trigger_count !== undefined && alert.trigger_count > 0
          ? t(` · 已触发 ${alert.trigger_count} 次`, ` · Triggered ${alert.trigger_count} times`)
          : ""}
      </p>
      <div className="alert-actions">
        {alert.status === "DRAFT" && (
          <button
            type="button"
            className="primary-button alert-action"
            onClick={() => void controller.confirm(alert.alert_id)}
          >
            {t("确认激活", "Confirm and activate")}
          </button>
        )}
        {alert.status === "DISABLED" ? (
          <button
            type="button"
            className="ghost-button alert-action"
            onClick={() => void controller.setEnabled(alert.alert_id, true)}
          >
            {t("启用", "Enable")}
          </button>
        ) : (
          <button
            type="button"
            className="ghost-button alert-action"
            onClick={() => void controller.setEnabled(alert.alert_id, false)}
          >
            {t("停用", "Disable")}
          </button>
        )}
        <button type="button" className="ghost-button alert-action" onClick={onEdit}>
          {t("编辑", "Edit")}
        </button>
        <button
          type="button"
          className="ghost-button alert-action alert-delete"
          onClick={() => {
            if (window.confirm(t(`删除预警 ${alert.alert_id}？`, `Delete alert ${alert.alert_id}?`))) {
              void controller.remove(alert.alert_id);
            }
          }}
        >
          {t("删除", "Delete")}
        </button>
      </div>
    </article>
  );
}

export interface AlertToastProps {
  trigger: AlertTriggerRecord;
  theme: Theme;
  onLocate: (trigger: AlertTriggerRecord) => void;
  onDismiss: () => void;
}

export function AlertToast({ trigger, theme, onLocate, onDismiss }: AlertToastProps) {
  useLocale();
  // DSH keeps inactive panels mounted. Their alerts still need a visible toast.
  return createPortal(
    <div className="talaria-desk alert-toast" data-theme={theme} role="status" data-testid="alert-toast">
      <button type="button" className="alert-toast-main" onClick={() => onLocate(trigger)}>
        <span className="alert-toast-title">{t("预警触发", "Alert triggered")}</span>
        <span className="alert-toast-message">{triggerSummary(trigger)}</span>
        <span className="alert-toast-meta">
          {trigger.instrument_id} · {trigger.timeframe}
          {trigger.snapshot_id !== "" ? t(" · 点击查看快照", " · View snapshot") : ""}
        </span>
      </button>
      <button type="button" className="alert-toast-close" aria-label={t("关闭预警通知", "Dismiss alert notification")} onClick={onDismiss}>
        ×
      </button>
    </div>,
    document.body,
  );
}
