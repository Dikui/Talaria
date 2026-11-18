import { useEffect, useMemo, useState, type FormEvent } from "react";
import type {
  AlertConditionKind,
  AlertDirection,
  AlertMetric,
  AlertOverlayMode,
  AlertRecord,
  AlertTriggerRecord,
  DrawingRecord,
  IndicatorName,
  InstrumentSpec,
} from "@talaria/contracts";

import { KIND_LABELS } from "../drawings/tools";
import { INDICATOR_LABELS } from "../indicators/config";
import {
  ALERT_INDICATORS,
  ALERT_OVERLAY_KINDS,
  ALERT_STATUS_LABELS,
  ALERT_TABS,
  METRIC_LABELS,
  alertsForTab,
  buildAlertCreate,
  conditionSummary,
  defaultAlertForm,
  drawingKindFor,
  formatCooldown,
  formFromRecord,
  relativeTime,
  statusToTab,
  tabCounts,
  triggerSummary,
  validateAlertForm,
  type AlertFormState,
  type AlertTabId,
} from "./model";
import type { AlertsController } from "./useAlerts";

const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"];

export interface AlertFormSeed {
  nonce: number;
  drawingId?: string;
  instrumentId?: string;
  timeframe?: string;
}

export interface AlertPanelProps {
  controller: AlertsController;
  instruments: InstrumentSpec[];
  drawings: DrawingRecord[];
  currentInstrument: string;
  currentTimeframe: string;
  formSeed: AlertFormSeed | null;
  onLocate: (trigger: AlertTriggerRecord) => void;
  onClose?: () => void;
}

export function AlertPanel({
  controller,
  instruments,
  drawings,
  currentInstrument,
  currentTimeframe,
  formSeed,
  onLocate,
  onClose,
}: AlertPanelProps) {
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
        { drawingId: formSeed.drawingId },
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
            condition: payload.condition,
            note: payload.note,
            repeat: payload.repeat,
            cooldown_ms: payload.cooldown_ms,
          });
    if (result === null) {
      setFormError(controller.error ?? "保存预警失败");
      return;
    }
    setFormOpen(false);
    setEditingId(null);
    setTab(statusToTab(result.status));
  };

  const changeKind = (kind: AlertConditionKind) => {
    setForm((current) => ({
      ...current,
      kind,
      drawingId:
        kind === "OVERLAY_CROSS" && current.drawingId === ""
          ? (selectableDrawings[0]?.drawing_id ?? "")
          : current.drawingId,
    }));
  };

  return (
    <aside className="workspace-copilot alerts-panel" data-testid="alerts-panel">
      <div className="alerts-head">
        <div>
          <h2 className="alerts-title">预警</h2>
          <p className="alerts-subtitle">阈值 · 指标 · 画线几何</p>
        </div>
        <div className="alerts-head-actions">
          <button
            type="button"
            className="ghost-button alerts-sound"
            aria-pressed={controller.soundEnabled}
            onClick={() => controller.setSoundEnabled(!controller.soundEnabled)}
            title="触发提示音"
          >
            {controller.soundEnabled ? "声音开" : "声音关"}
          </button>
          {onClose !== undefined && (
            <button
              type="button"
              className="ghost-button"
              aria-label="收起预警面板"
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
          新建预警
        </button>
      </div>

      {controller.error !== null && (
        <div className="alerts-error" role="alert" data-testid="alerts-error">
          <span>{controller.error}</span>
          <button type="button" aria-label="关闭预警错误" onClick={controller.clearError}>
            ×
          </button>
        </div>
      )}

      {formOpen && (
        <form className="alert-form" onSubmit={submit} data-testid="alert-form">
          <div className="alert-form-title">{editingId === null ? "新建预警" : "编辑预警"}</div>
          <label>
            标的
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
            周期
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
          <label>
            条件
            <select
              value={form.kind}
              onChange={(event) => changeKind(event.target.value as AlertConditionKind)}
            >
              <option value="METRIC_THRESHOLD">指标阈值</option>
              <option value="INDICATOR_SIGNAL">指标信号</option>
              <option value="OVERLAY_CROSS">画线触碰</option>
            </select>
          </label>

          {form.kind === "METRIC_THRESHOLD" && (
            <>
              <label>
                指标
                <select
                  value={form.metric}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      metric: event.target.value as AlertMetric,
                    }))
                  }
                >
                  {Object.entries(METRIC_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                阈值
                <input
                  value={form.threshold}
                  inputMode="decimal"
                  placeholder="例如 42000.5"
                  onChange={(event) =>
                    setForm((current) => ({ ...current, threshold: event.target.value }))
                  }
                />
              </label>
            </>
          )}

          {form.kind === "INDICATOR_SIGNAL" && (
            <>
              <label>
                指标
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
                      {INDICATOR_LABELS[name] ?? name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                周期
                <input
                  type="number"
                  min={1}
                  max={5000}
                  value={form.indicatorPeriod}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      indicatorPeriod: Number(event.target.value),
                    }))
                  }
                />
              </label>
              <label>
                对比指标
                <select
                  value={form.compareIndicator}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      compareIndicator: event.target.value as IndicatorName | "",
                    }))
                  }
                >
                  <option value="">无（用阈值）</option>
                  {ALERT_INDICATORS.map((name) => (
                    <option key={name} value={name}>
                      {INDICATOR_LABELS[name] ?? name}
                    </option>
                  ))}
                </select>
              </label>
              {form.compareIndicator === "" ? (
                <label>
                  阈值
                  <input
                    value={form.threshold}
                    inputMode="decimal"
                    placeholder="例如 70"
                    onChange={(event) =>
                      setForm((current) => ({ ...current, threshold: event.target.value }))
                    }
                  />
                </label>
              ) : (
                <label>
                  对比周期
                  <input
                    type="number"
                    min={1}
                    max={5000}
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
            </>
          )}

          {form.kind === "OVERLAY_CROSS" && (
            <>
              <label>
                画线
                <select
                  value={form.drawingId}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, drawingId: event.target.value }))
                  }
                >
                  <option value="">选择画线</option>
                  {selectableDrawings.map((drawing) => (
                    <option key={drawing.drawing_id} value={drawing.drawing_id}>
                      {KIND_LABELS[drawing.kind]} · {drawing.drawing_id.slice(4, 10)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                触发方式
                <select
                  value={form.overlayMode}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      overlayMode: event.target.value as AlertOverlayMode,
                    }))
                  }
                >
                  <option value="CLOSE_BREAK">收盘突破</option>
                  <option value="TOUCH">盘中触碰</option>
                </select>
              </label>
            </>
          )}

          <label>
            方向
            <select
              value={form.direction}
              onChange={(event) =>
                setForm((current) => ({
                  ...current,
                  direction: event.target.value as AlertDirection,
                }))
              }
            >
              <option value="ABOVE">上穿 / 高于</option>
              <option value="BELOW">下穿 / 低于</option>
            </select>
          </label>
          <label>
            冷却（秒）
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
            备注
            <textarea
              value={form.note}
              maxLength={500}
              rows={2}
              placeholder="可选，最多 500 字"
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
              重复提醒
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
              保存为草案
            </label>
          </div>
          {formError !== null && (
            <div className="alert-form-error" role="alert">
              {formError}
            </div>
          )}
          <div className="alert-form-actions">
            <button type="submit" className="primary-button">
              保存
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
              取消
            </button>
          </div>
        </form>
      )}

      <div className="alerts-tabs" role="tablist" aria-label="预警规则分类">
        {ALERT_TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={tab === item.id}
            className={`alerts-tab${tab === item.id ? " is-active" : ""}`}
            onClick={() => setTab(item.id)}
          >
            {item.label}
            <span className="alerts-tab-count">{counts[item.id]}</span>
          </button>
        ))}
      </div>

      <div className="alerts-list" data-testid="alerts-list">
        {controller.loading ? (
          <p className="alerts-empty">正在加载预警…</p>
        ) : visibleAlerts.length === 0 ? (
          <div className="alerts-empty" data-testid="alerts-empty">
            <strong>{ALERT_TABS.find((item) => item.id === tab)?.label}暂无规则</strong>
            <span>新建预警，或从画线属性一键挂载。</span>
          </div>
        ) : (
          visibleAlerts.map((alert) => (
            <AlertCard
              key={alert.alert_id}
              alert={alert}
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
          触发历史 {controller.triggers.length}
          <span>{showHistory ? "收起" : "展开"}</span>
        </button>
        {showHistory &&
          (controller.triggers.length === 0 ? (
            <p className="alerts-empty" data-testid="triggers-empty">
              暂无触发记录。
            </p>
          ) : (
            <ul className="trigger-list" data-testid="trigger-list">
              {controller.triggers.map((trigger) => (
                <li key={trigger.trigger_id} className="trigger-item">
                  <button
                    type="button"
                    className="trigger-main"
                    onClick={() => onLocate(trigger)}
                    title="定位到触发时刻"
                  >
                    <span className="trigger-message">{triggerSummary(trigger)}</span>
                    <span className="trigger-meta">
                      {trigger.instrument_id} · {trigger.timeframe} ·{" "}
                      {relativeTime(trigger.triggered_ts, now)}
                      {trigger.snapshot_id !== "" ? " · 有快照" : ""}
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
  alert: AlertRecord;
  drawings: DrawingRecord[];
  now: number;
  controller: AlertsController;
  onEdit: () => void;
}

function AlertCard({ alert, drawings, now, controller, onEdit }: AlertCardProps) {
  const kind = drawingKindFor(alert.condition.drawing_id ?? "", drawings);
  return (
    <article
      className={`alert-item alert-item--${alert.status.toLowerCase()}${
        controller.toast?.alert_id === alert.alert_id ? " alert-item--recent" : ""
      }`}
    >
      <header className="alert-item-head">
        <span className={`alert-status alert-status--${alert.status.toLowerCase()}`}>
          {ALERT_STATUS_LABELS[alert.status]}
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
        {alert.repeat ? `重复 · 冷却 ${formatCooldown(alert.cooldown_ms)}` : "单次触发"}
        {alert.next_arm_ts !== undefined && alert.next_arm_ts !== null
          ? ` · ${relativeTime(alert.next_arm_ts, now).replace("前", "后")}回到待命`
          : ""}
        {alert.trigger_count !== undefined && alert.trigger_count > 0
          ? ` · 已触发 ${alert.trigger_count} 次`
          : ""}
      </p>
      <div className="alert-actions">
        {alert.status === "DRAFT" && (
          <button
            type="button"
            className="primary-button alert-action"
            onClick={() => void controller.confirm(alert.alert_id)}
          >
            确认激活
          </button>
        )}
        {alert.status === "DISABLED" ? (
          <button
            type="button"
            className="ghost-button alert-action"
            onClick={() => void controller.setEnabled(alert.alert_id, true)}
          >
            启用
          </button>
        ) : (
          <button
            type="button"
            className="ghost-button alert-action"
            onClick={() => void controller.setEnabled(alert.alert_id, false)}
          >
            停用
          </button>
        )}
        <button type="button" className="ghost-button alert-action" onClick={onEdit}>
          编辑
        </button>
        <button
          type="button"
          className="ghost-button alert-action alert-delete"
          onClick={() => {
            if (window.confirm(`删除预警 ${alert.alert_id}？`)) {
              void controller.remove(alert.alert_id);
            }
          }}
        >
          删除
        </button>
      </div>
    </article>
  );
}

export interface AlertToastProps {
  trigger: AlertTriggerRecord;
  onLocate: (trigger: AlertTriggerRecord) => void;
  onDismiss: () => void;
}

export function AlertToast({ trigger, onLocate, onDismiss }: AlertToastProps) {
  return (
    <div className="alert-toast" role="status" data-testid="alert-toast">
      <button type="button" className="alert-toast-main" onClick={() => onLocate(trigger)}>
        <span className="alert-toast-title">预警触发</span>
        <span className="alert-toast-message">{triggerSummary(trigger)}</span>
        <span className="alert-toast-meta">
          {trigger.instrument_id} · {trigger.timeframe}
          {trigger.snapshot_id !== "" ? " · 点击查看快照" : ""}
        </span>
      </button>
      <button type="button" className="alert-toast-close" aria-label="关闭预警通知" onClick={onDismiss}>
        ×
      </button>
    </div>
  );
}
