import { useEffect, useId, useMemo, useRef, useState } from "react";
import { RULE_EDITOR_SCHEMA as schema } from "@talaria/contracts";
import type { DrawingRecord, KeyLevel, Rule, RulePreviewResponse } from "@talaria/contracts";
import { api } from "../../lib/api";
import { ALERT_OVERLAY_KINDS, errorMessage } from "../alerts/model";
import type { IndicatorDefaults } from "../indicators/config";
import { RuleEditorForm, RuleTiming } from "./RuleEditor";
import { ruleIndicatorLabel } from "./ruleChart";
import { expressionComplete, type Expression, type Timing } from "./ruleForm";

export interface ChartRulePreview {
  snapshotId: string;
  response: RulePreviewResponse;
}

interface ChartRuleEditorProps {
  instrumentId: string;
  timeframe: string;
  snapshotId: string | null;
  loadedSlots: number;
  defaults: IndicatorDefaults;
  drawings: readonly DrawingRecord[];
  levels: readonly KeyLevel[];
  pricePrecision: number;
  onPreview: (preview: ChartRulePreview | null) => void;
  onSaved: () => void;
  onViewAlerts: () => void;
}

export function ChartRuleEditor({
  instrumentId, timeframe, snapshotId, loadedSlots, defaults, drawings, levels, pricePrecision,
  onPreview, onSaved, onViewAlerts,
}: ChartRuleEditorProps) {
  const panelId = useId();
  const [expanded, setExpanded] = useState(false);
  const [timing, setTiming] = useState<Timing>("bar_close");
  const [expressions, setExpressions] = useState<Record<Timing, Expression | null>>({
    bar_close: null, event: null,
  });
  const [preview, setPreview] = useState<{
    snapshotId: string; fingerprint: string; limit: number; response?: RulePreviewResponse; error?: string;
  } | null>(null);
  const [retry, setRetry] = useState(0);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [savedRule, setSavedRule] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<{ fingerprint: string; message: string } | null>(null);
  const expression = expressions[timing];
  const availableDrawings = drawings.filter((drawing) =>
    drawing.instrument_id === instrumentId && drawing.deleted_at == null && ALERT_OVERLAY_KINDS.includes(drawing.kind));
  const availableLevels = levels.filter((level) => level.instrument_id === instrumentId);
  const complete = expression !== null && expressionComplete(
    expression, new Set(availableDrawings.map((drawing) => drawing.drawing_id)),
    new Set(availableLevels.map((level) => level.level_id)),
  );
  const rule = useMemo<Rule | null>(
    () => complete && expression ? { version: schema.version, timing, expression } : null,
    [complete, timing, expression],
  );
  const fingerprint = rule === null ? "" : JSON.stringify(rule);
  const limit = Math.max(1, Math.min(loadedSlots, schema.previewLimit));
  const current = preview?.snapshotId === snapshotId && preview.fingerprint === fingerprint && preview.limit === limit
    ? preview
    : null;
  const response = current?.response;
  const saved = savedRule === fingerprint;
  const warmup = response?.indicators.filter((line) =>
    line.points.every((point) => point.values.value == null)) ?? [];

  useEffect(() => {
    onPreview(null);
    if (!snapshotId || !rule) return;
    const abort = new AbortController();
    const identity = { snapshotId, fingerprint, limit };
    const timer = window.setTimeout(() => {
      api.previewRule(snapshotId, { timeframe, rule, limit }, abort.signal)
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
  }, [snapshotId, rule, fingerprint, timeframe, limit, retry, onPreview]);

  async function save() {
    if (!response || savingRef.current || saved) return;
    savingRef.current = true;
    setSaving(true);
    setSaveError(null);
    try {
      await api.createAlert({ instrument_id: instrumentId, timeframe, condition: response.rule });
      setSavedRule(fingerprint);
      onSaved();
    } catch (error) {
      setSaveError({ fingerprint, message: errorMessage(error) });
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }

  let status = "";
  if (expression !== null) {
    if (!complete) status = "请补全条件并检查数值范围。";
    else if (!snapshotId) status = "等待图表快照。";
    else if (current?.error) status = current.error;
    else if (!response) status = "正在计算规则…";
    else if (timing === "event") status = "快照没有盘中行情事件。预警保存后将按实时行情求值。";
    else status = `当前快照 · ${response.signals.length} 根 K 线命中${loadedSlots > limit ? `（最近 ${limit} 个时间槽）` : ""}`;
  }

  return (
    <section className="chart-rule-editor" aria-label="图表规则编辑器">
      <div className="rule-editor-header">
        <button type="button" className={`drawing-action${expanded ? " is-active" : ""}`}
          aria-expanded={expanded} aria-controls={panelId} onClick={() => setExpanded(!expanded)}>
          图表规则
        </button>
        {expanded && <RuleTiming timing={timing} onChange={setTiming} />}
        <span className="rule-status" role="status">{status}</span>
        {current?.error && (
          <button type="button" className="drawing-action" onClick={() => {
            setPreview(null);
            setRetry((value) => value + 1);
          }}>重试预览</button>
        )}
      </div>
      <div id={panelId} hidden={!expanded}>
        <div className="rule-editor-body">
          <RuleEditorForm timing={timing} expression={expression}
            defaults={defaults} drawings={availableDrawings} levels={availableLevels} pricePrecision={pricePrecision}
            onChange={(node) => setExpressions((previous) => ({ ...previous, [timing]: node }))} />
        </div>
        <div className="rule-editor-footer">
          <button type="button" className="primary-button" disabled={!response || saving || saved}
            aria-busy={saving} onClick={() => void save()}>保存为预警</button>
          {saved && (
            <button type="button" className="drawing-action" onClick={onViewAlerts}>已保存 · 查看预警</button>
          )}
          <span className="rule-status" role="status">
            {saving ? "正在保存…" : saveError?.fingerprint === fingerprint ? saveError.message : ""}
          </span>
          {warmup.length > 0 && (
            <p className="rule-warmup" role="status">
              指标暂无可用值（可能仍在预热） · {warmup.map(ruleIndicatorLabel).join("、")}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
