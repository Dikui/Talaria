import { useEffect, useId, useMemo, useRef, useState } from "react";
import { RULE_EDITOR_SCHEMA as schema } from "@talaria/contracts";
import type { AlertRecord, DrawingRecord, KeyLevel, Rule, RulePreviewResponse, RulePreviewSignal } from "@talaria/contracts";
import { api } from "../../lib/api";
import { t, useLocale } from "../../lib/i18n";
import { errorMessage } from "../alerts/model";
import type { IndicatorDefaults } from "../indicators/config";
import { RuleEditorForm, RuleTiming } from "./RuleEditor";
import { ruleIndicatorLabel } from "./ruleChart";
import { expressionComplete, type Expression, type Timing } from "./ruleForm";

import { RuleDrafts, ruleDrawingOptions, savedAlertExists } from "./ruleDrafts";

export interface ChartRulePreview {
  snapshotId: string;
  response: RulePreviewResponse;
}

interface ChartRuleEditorProps {
  drafts: RuleDrafts;
  scope: string;
  alerts: readonly AlertRecord[];
  instrumentId: string;
  timeframe: string;
  snapshotId: string | null;
  loadedSlots: number;
  defaults: IndicatorDefaults;
  drawings: readonly DrawingRecord[];
  levels: readonly KeyLevel[];
  levelSnapshotId: string | undefined;
  pricePrecision: number;
  onPreview: (preview: ChartRulePreview | null) => void;
  onSaved: () => Promise<void>;
  onViewAlerts: () => void;
}

export function RuleHits({ conditions, triggers }: {
  conditions: RulePreviewSignal[]; triggers: RulePreviewSignal[];
}) {
  useLocale();
  if (!conditions.length && !triggers.length) return null;
  return <details className="rule-hit-details">
    <summary>{t("命中明细", "Matched details")}</summary>
    {([[t("条件成立", "Conditions met"), conditions], [t("触发预警", "Alert triggers"), triggers]] as const).map(([kind, hits]) => (
      <section key={kind} aria-label={kind}><strong>{kind}{t("（", " (")}{hits.length}{t("）", ")")}</strong><ul>
        {hits.map((hit, index) => {
          const time = new Date(hit.observed_at).toISOString();
          return <li tabIndex={0} key={`${hit.timestamp}/${hit.observed_at}/${index}`}>
            {kind} · {t("柱", "Bar")} <time dateTime={new Date(hit.timestamp).toISOString()}>{new Date(hit.timestamp).toISOString()}</time>
            {" · "}{t("观察", "Observed")} <time dateTime={time}>{time}</time> · {t("价格", "Price")} {hit.price ?? t("未记录", "not recorded")}
          </li>;
        })}
      </ul></section>
    ))}
  </details>;
}

export function ChartRuleEditor({
  drafts, scope, alerts, instrumentId, timeframe, snapshotId, loadedSlots, defaults, drawings, levels, levelSnapshotId, pricePrecision,
  onPreview, onSaved, onViewAlerts,
}: ChartRuleEditorProps) {
  useLocale();
  const panelId = useId();
  const [expanded, setExpanded] = useState(false);
  const [draft, setDraft] = useState(() => drafts.read(scope));
  const [client] = useState(() => crypto.randomUUID());
  const { timing, expressions } = draft;
  function edit(timing: Timing, expressions: Record<Timing, Expression | null>) {
    const next = { timing, expressions };
    drafts.write(scope, next);
    setDraft(next);
  }
  const [preview, setPreview] = useState<{
    snapshotId: string; fingerprint: string; limit: number; references: string; response?: RulePreviewResponse; error?: string;
  } | null>(null);
  const [retry, setRetry] = useState(0);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [saveError, setSaveError] = useState<{ fingerprint: string; message: string } | null>(null);
  const expression = expressions[timing];
  const availableDrawings = ruleDrawingOptions(drawings, instrumentId);
  const availableLevels = levels.filter((level) => level.instrument_id === instrumentId);
  const complete = expression !== null && expressionComplete(
    expression, new Set(availableDrawings.map((drawing) => drawing.drawing_id)),
  );
  const rule = useMemo<Rule | null>(
    () => complete && expression ? { version: schema.version, timing, expression } : null,
    [complete, timing, expression],
  );
  const fingerprint = rule === null ? "" : JSON.stringify(rule);
  const limit = Math.max(1, Math.min(loadedSlots, schema.previewLimit));
  const referenceVersion = JSON.stringify([
    fingerprint.includes('"level_id"') ? [levelSnapshotId, levels.map(level => [level.level_id, level.kind])] : null,
    fingerprint.includes('"drawing_id"') ? availableDrawings.map(drawing => [drawing.drawing_id, drawing.version]) : null,
  ]);
  const current = preview?.snapshotId === snapshotId && preview.fingerprint === fingerprint && preview.limit === limit
    && preview.references === referenceVersion
    ? preview
    : null;
  const response = current?.response;
  const savedFingerprint = response ? JSON.stringify(response.rule) : "";
  const saved = savedAlertExists(drafts.savedId(scope, savedFingerprint), alerts);
  const missingLevels = response?.unavailable_current_levels ?? [];
  const warmup = response?.indicators.filter((line) =>
    line.points.every((point) => point.values.value == null)) ?? [];

  useEffect(() => {
    setPreview(null);
    onPreview(null);
    if (!snapshotId || !rule) return;
    const abort = new AbortController();
    const identity = { snapshotId, fingerprint, limit, references: referenceVersion };
    const timer = window.setTimeout(() => {
      api.previewRule(snapshotId, { timeframe, rule, limit }, abort.signal, client)
        .then((response) => {
          if (abort.signal.aborted) return;
          setPreview({ ...identity, response });
          onPreview({ snapshotId, response });
        })
        .catch((error) => {
          if (!abort.signal.aborted) setPreview({ ...identity, error: errorMessage(error) });
        });
    }, 250);
    return () => {
      window.clearTimeout(timer);
      abort.abort();
    };
  }, [snapshotId, rule, fingerprint, timeframe, limit, retry, onPreview, client, referenceVersion]);

  async function save() {
    if (!response || savingRef.current || saved || missingLevels.length) return;
    savingRef.current = true;
    setSaving(true);
    setSaveError(null);
    try {
      const alert = await api.createAlert({ instrument_id: instrumentId, timeframe, condition: response.rule });
      await onSaved();
      drafts.rememberSaved(scope, savedFingerprint, alert.alert_id);
    } catch (error) {
      setSaveError({ fingerprint, message: errorMessage(error) });
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }

  let status = "";
  if (expression !== null) {
    if (!complete) status = t("请补全条件并检查数值范围。", "Complete the conditions and check the numeric ranges.");
    else if (!snapshotId) status = t("等待图表快照。", "Waiting for the chart snapshot.");
    else if (current?.error) status = current.error;
    else if (!response) status = t("正在计算规则…", "Evaluating rule…");
    else if (response.history_incomplete) status = t("部分时段的关键位历史版本不可用。图上和明细仅列出已确认的结果。", "Key level history is unavailable for some periods. Only confirmed results are shown on the chart and in the details.");
    else status = t(
      `${timing === "event" ? "快照内已记录事件 · " : ""}${response.condition_true.length} 根条件成立 · ${response.would_trigger.length} 次触发预警${loadedSlots > limit ? `（最近 ${limit} 个时间槽）` : ""}`,
      `${timing === "event" ? "Events recorded in snapshot · " : ""}${response.condition_true.length} bars matched · ${response.would_trigger.length} alert triggers${loadedSlots > limit ? ` (latest ${limit} time slots)` : ""}`,
    );
  }

  return (
    <section className="chart-rule-editor" aria-label={t("图表规则编辑器", "Chart rule editor")}>
      <div className="rule-editor-header">
        <button type="button" className={`drawing-action${expanded ? " is-active" : ""}`}
          aria-expanded={expanded} aria-controls={panelId} onClick={() => setExpanded(!expanded)}>
          {t("图表规则", "Chart rules")}
        </button>
        {expanded && <RuleTiming timing={timing} onChange={next => edit(next, expressions)} />}
        <span className="rule-status" role="status">{status}</span>
        {current?.error && (
          <button type="button" className="drawing-action" onClick={() => {
            setPreview(null);
            setRetry((value) => value + 1);
          }}>{t("重试预览", "Retry preview")}</button>
        )}
      </div>
      <div className="rule-editor-content" id={panelId} hidden={!expanded}>
        <div className="rule-editor-body">
          <RuleEditorForm timing={timing} expression={expression}
            defaults={defaults} drawings={availableDrawings} levels={availableLevels} pricePrecision={pricePrecision}
            onChange={node => edit(timing, { ...expressions, [timing]: node })} />
        </div>
        <div className="rule-editor-footer">
          <button type="button" className="primary-button" disabled={!response || saving || saved || missingLevels.length > 0}
            aria-busy={saving} onClick={() => void save()}>{t("保存为预警", "Save as alert")}</button>
          {missingLevels.length > 0 && <span role="status">{t(`当前版本缺少关键位（${missingLevels.join("、")}），请选择当前关键位后保存。`, `Missing key levels (${missingLevels.join(", ")}), select current key levels before saving.`)}</span>}
          {saved && (
            <button type="button" className="drawing-action" onClick={onViewAlerts}>{t("已保存 · 查看预警", "Saved · View alerts")}</button>
          )}
          <span className="rule-status" role="status">
            {saving ? t("正在保存…", "Saving…") : saveError?.fingerprint === fingerprint ? saveError.message : ""}
          </span>
          {warmup.length > 0 && (
            <p className="rule-warmup" role="status">
              {t("指标暂无可用值（可能仍在预热） · ", "Indicator values unavailable (possibly warming up) · ")}{warmup.map(ruleIndicatorLabel).join(t("、", ", "))}
            </p>
          )}
        </div>
        {response && <RuleHits conditions={response.condition_true} triggers={response.would_trigger} />}
      </div>
    </section>
  );
}
