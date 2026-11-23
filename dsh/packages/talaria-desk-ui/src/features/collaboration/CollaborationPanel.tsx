import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { Chart } from "klinecharts";

import type {
  CollabWindow,
  CompactBar,
  ConsultationRecord,
  DrawingRecord,
  EvidenceRef,
  SetupInsight,
  SnapshotMeta,
} from "@talaria/contracts";

import { getLocale, t, useLocale } from "../../lib/i18n";
import { timeWindow } from "../drawings/viewport";
import { visiblePriceWindow } from "../drawings/projection";
import type { CollaborationView } from "./useCollaborationStatus";
import { useConsultation } from "./useConsultation";
import {
  abstainCopy,
  consultationStatusLabel,
  directionLabel,
  evidenceLabel,
  groupEvidence,
  usageLine,
} from "./model";

const EXAMPLES = [
  {
    zh: "现在更适合等回踩，还是等突破确认？",
    en: "Should I wait for a pullback or for breakout confirmation?",
  },
  {
    zh: "框选区间内有哪些关键位值得关注？",
    en: "Which key levels in the selected range deserve attention?",
  },
  {
    zh: "如果入场，止损和目标应该放在哪里？",
    en: "If I enter, where should the stop and targets go?",
  },
] as const;

const LOCAL_ERROR = {
  chartNotReady: "chart_not_ready",
  freezeFailed: "freeze_failed",
  requestNotSent: "request_not_sent",
} as const;

function localErrorText(code: string): string {
  switch (code) {
    case LOCAL_ERROR.chartNotReady:
      return t("图表尚未就绪，无法冻结可视范围；请稍后再试。", "The chart is not ready, so the visible range cannot be frozen. Try again shortly.");
    case LOCAL_ERROR.freezeFailed:
      return t("快照冻结失败；图表与画线仍可继续操作。", "Snapshot freezing failed. The chart and drawings remain usable.");
    case LOCAL_ERROR.requestNotSent:
      return t("会诊请求未发出", "The consultation request was not sent");
    default:
      return code;
  }
}

const DATE_FORMAT: Intl.DateTimeFormatOptions = {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
};

function formatTs(ms: number): string {
  return new Intl.DateTimeFormat(getLocale() === "zh" ? "zh-CN" : "en-US", DATE_FORMAT).format(
    new Date(ms),
  );
}

function formatRange(window: CollabWindow): string {
  return `${formatTs(window.from_ts)} → ${formatTs(window.to_ts)} · ${window.price_min}–${window.price_max}`;
}

function EvidenceFold({ refs }: { refs: readonly EvidenceRef[] | undefined }) {
  const locale = useLocale();
  const groups = useMemo(() => groupEvidence(refs ?? [], locale), [refs, locale]);
  const count = refs?.length ?? 0;
  if (count === 0) return null;
  return (
    <details className="evidence-fold" data-testid="evidence-fold">
      <summary>
        {t(`证据 ${count} 条 · 展开核对`, `${count} evidence items · expand to verify`)}
      </summary>
      <div className="evidence-groups">
        {groups.map((group) => (
          <section key={group.kind} className="evidence-group">
            <h4>{group.label}</h4>
            <ul>
              {group.items.map((ref) => (
                <li key={ref.evidence_id}>
                  <span className="evidence-label">{evidenceLabel(ref)}</span>
                  {ref.detail !== "" && (
                    <span className="evidence-detail">{ref.detail}</span>
                  )}
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
    </details>
  );
}

function InsightCard({ insight }: { insight: SetupInsight }) {
  const locale = useLocale();
  return (
    <div className="insight-card" data-testid="insight-card">
      <div className="insight-head">
        <span className={`direction-badge direction-badge--${insight.direction.toLowerCase()}`}>
          {directionLabel(insight.direction, locale)}
        </span>
        <span className="insight-note">{t("观察建议 · 非订单", "Observation · not an order")}</span>
      </div>
      <p className="insight-summary">{insight.summary}</p>
      <dl className="insight-levels">
        <div>
          <dt>{t("入场带", "Entry zone")}</dt>
          <dd>
            {insight.entry_low}–{insight.entry_high}
          </dd>
        </div>
        <div>
          <dt>{t("证伪止损", "Invalidation stop")}</dt>
          <dd>{insight.invalidation_stop}</dd>
        </div>
        <div>
          <dt>{t("止盈目标", "Take-profit targets")}</dt>
          <dd>{insight.take_profit.join(" / ")}</dd>
        </div>
        {insight.max_position !== null && insight.max_position !== undefined && (
          <div>
            <dt>{t("持仓上限", "Max position")}</dt>
            <dd>{insight.max_position}</dd>
          </div>
        )}
        {insight.valid_until !== null && insight.valid_until !== undefined && (
          <div>
            <dt>{t("有效期至", "Valid until")}</dt>
            <dd>{formatTs(insight.valid_until)}</dd>
          </div>
        )}
      </dl>
      <EvidenceFold refs={insight.evidence_refs} />
    </div>
  );
}

function TurnCard({ record, onStageDraft }: {
  record: ConsultationRecord;
  onStageDraft?: (record: ConsultationRecord) => void;
}) {
  const locale = useLocale();
  const abstain = record.abstain;
  return (
    <article
      className={`consultation-turn consultation-turn--${record.status.toLowerCase()}`}
      data-testid="consultation-turn"
    >
      <div className="consultation-question">
        <span className="question-mark">{t("问", "Q")}</span>
        <p>{record.package.question}</p>
        <time dateTime={new Date(record.created_ts).toISOString()}>
          {formatTs(record.created_ts)}
        </time>
      </div>
      <div className="consultation-answer">
        <span className="consultation-status">{consultationStatusLabel(record, locale)}</span>
        {record.status === "PENDING" && (
          <div className="consultation-pending" role="status">
            <span className="pending-dots" aria-hidden="true">
              <i />
              <i />
              <i />
            </span>
            {t("协同宿主正在核对只读工具结果；图表与画线不受影响。", "The collaboration host is checking read-only tool results. The chart and drawings are unaffected.")}
          </div>
        )}
        {record.status === "READY" && record.insight !== null && record.insight !== undefined && (
          <>
            <InsightCard insight={record.insight} />
            {onStageDraft && record.insight.direction !== "NEUTRAL" &&
              <button type="button" className="ghost-button" onClick={() => onStageDraft(record)}>{t("存为模拟盘草案", "Stage as paper draft")}</button>}
          </>
        )}
        {record.status === "ABSTAIN" && abstain !== null && abstain !== undefined && (
          <div
            className="abstain-card"
            data-testid="abstain-card"
            data-reason={abstain.reason_code}
            role="status"
          >
            <div className="abstain-head">
              <span className="abstain-mark" aria-hidden="true">
                !
              </span>
              <strong>{abstainCopy(abstain, locale).title}</strong>
              <code>{abstain.reason_code}</code>
            </div>
            <p>{abstain.message}</p>
            <p className="abstain-hint">{abstainCopy(abstain, locale).hint}</p>
            <EvidenceFold refs={abstain.evidence_refs} />
          </div>
        )}
        {record.status === "FAILED" && (
          <div className="consultation-failed" role="alert">
            {record.error ?? t("协同会诊失败；本地图表不受影响。", "Consultation failed. The local chart is unaffected.")}
          </div>
        )}
        {record.dsh_session_id !== null && record.dsh_session_id !== undefined && (
          <p className="consultation-binding">
            {t(`宿主回合 ${record.dsh_session_id.slice(0, 12)}`, `Host turn ${record.dsh_session_id.slice(0, 12)}`)} · turn{" "}
            {record.dsh_turn_id ?? "—"}
          </p>
        )}
      </div>
    </article>
  );
}

export interface CollaborationDraft {
  nonce: number;
  question: string;
}

export interface CollaborationPanelProps {
  snapshot: SnapshotMeta | null;
  freezeSnapshot: () => Promise<SnapshotMeta | null>;
  instrumentId: string;
  timeframe: string;
  pricePrecision: number;
  chart: Chart | null;
  bars: CompactBar[];
  drawings: DrawingRecord[];
  selection: CollabWindow | null;
  selecting: boolean;
  onToggleSelecting: () => void;
  onClearSelection: () => void;
  hostView: CollaborationView;
  readOnlyViewport: boolean;
  draft?: CollaborationDraft | null;
  tabBar?: ReactNode;
  onClose: () => void;
  onStageDraft?: (record: ConsultationRecord) => void;
}

export function CollaborationPanel({
  snapshot,
  freezeSnapshot,
  instrumentId,
  timeframe,
  pricePrecision,
  chart,
  bars,
  drawings,
  selection,
  selecting,
  onToggleSelecting,
  onClearSelection,
  hostView,
  readOnlyViewport,
  draft = null,
  tabBar,
  onClose,
  onStageDraft,
}: CollaborationPanelProps) {
  const locale = useLocale();
  const consultation = useConsultation();
  const [question, setQuestion] = useState("");
  const [freezing, setFreezing] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (draft !== null) {
      setQuestion(draft.question);
    }
  }, [draft?.nonce, draft?.question]);

  const ordered = useMemo(
    () => [...consultation.records].reverse(),
    [consultation.records],
  );

  useEffect(() => {
    const list = listRef.current;
    if (list !== null) list.scrollTop = list.scrollHeight;
  }, [ordered.length, consultation.records]);

  const visibleWindow = (): CollabWindow | null => {
    if (chart === null || bars.length === 0) return null;
    const times = timeWindow(chart.getVisibleRange(), bars);
    const prices = visiblePriceWindow(chart, pricePrecision);
    if (times === null || prices === null) return null;
    return {
      from_ts: times.from_ts,
      to_ts: times.to_ts,
      price_min: prices.price_min,
      price_max: prices.price_max,
    };
  };

  const submit = async () => {
    const text = question.trim();
    if (text === "" || freezing || consultation.submitting) return;
    const visible = visibleWindow();
    if (visible === null) {
      setLocalError(LOCAL_ERROR.chartNotReady);
      return;
    }
    setLocalError(null);
    setFreezing(true);
    try {
      const frozen = await freezeSnapshot();
      if (frozen === null) {
        setLocalError(LOCAL_ERROR.freezeFailed);
        return;
      }
      await consultation.start({
        snapshot_id: frozen.snapshot_id,
        instrument_id: instrumentId,
        timeframe,
        question: text,
        visible,
        selection,
        drawing_ids: drawings.slice(0, 200).map((item) => item.drawing_id),
      });
      setQuestion("");
    } catch (caught) {
      setLocalError(caught instanceof Error ? caught.message : LOCAL_ERROR.requestNotSent);
    } finally {
      setFreezing(false);
    }
  };

  const busy = freezing || consultation.submitting;
  const disabled = busy || question.trim() === "" || bars.length === 0;

  return (
    <aside
      className="workspace-copilot collaboration-panel"
      data-testid="collaboration-panel"
      aria-label={t("协同会诊", "Collaboration consultation")}
    >
      {tabBar}
      <header className="collaboration-head">
        <div>
          <h2 className="collaboration-title">{t("协同会诊", "Collaboration consultation")}</h2>
          <p className="collaboration-subtitle">
            {t("冻结快照 · 只读工具 · Setup Insight", "Frozen snapshot · read-only tools · Setup Insight")}
          </p>
        </div>
        <span
          className={`host-badge host-badge--${hostView.tone}`}
          data-testid="collab-host-status"
          title={hostView.status?.message ?? t("正在检测协同宿主", "Checking collaboration host")}
        >
          {hostView.label}
        </span>
      </header>

      {hostView.tone === "warning" && (
        <div className="collaboration-host-warning" role="status">
          {t("协同宿主忙碌或离线：图表、画线、预警与快照冻结仍然可用；会诊将快速 Abstain 或稍后重试。", "The collaboration host is busy or offline. The chart, drawings, alerts, and snapshot freezing still work; consultations will quickly abstain or can be retried later.")}
        </div>
      )}

      <div className="collaboration-selection-row">
        {!readOnlyViewport && (
          <button
            type="button"
            className={`ghost-button${selecting ? " is-active" : ""}`}
            aria-pressed={selecting}
            onClick={onToggleSelecting}
            disabled={chart === null}
          >
            {selecting ? t("正在框选…", "Selecting…") : t("框选区间", "Select range")}
          </button>
        )}
        {selection === null ? (
          <span className="selection-empty">{t("未框选：使用整个可视范围", "No selection: using the full visible range")}</span>
        ) : (
          <>
            <span className="selection-summary" data-testid="selection-summary">
              {formatRange(selection)}
            </span>
            <button type="button" className="link-button" onClick={onClearSelection}>
              {t("清除", "Clear")}
            </button>
          </>
        )}
      </div>

      <div className="collaboration-flow" ref={listRef} data-testid="consultation-flow">
        {consultation.loading ? (
          <p className="collaboration-loading">{t("正在读取最近会诊…", "Loading recent consultations…")}</p>
        ) : consultation.records.length === 0 ? (
          <div className="collaboration-empty" data-testid="collaboration-empty">
            <div className="collaboration-empty-mark" aria-hidden="true">
              ◎
            </div>
            <h3>{t("还没有会诊记录", "No consultations yet")}</h3>
            <p>
              {t("冻结当前快照后，宿主会调用只读工具核对指标、关键位与画线，再给出带证据的观察建议；证据不足时会明确 Abstain。", "After freezing the current snapshot, the host checks indicators, key levels, and drawings with read-only tools, then returns evidence-backed observations. It abstains explicitly when evidence is insufficient.")}
            </p>
            <div className="collaboration-examples">
              {EXAMPLES.map((item) => (
                <button
                  key={item.zh}
                  type="button"
                  className="example-chip"
                  onClick={() => setQuestion(locale === "zh" ? item.zh : item.en)}
                >
                  {locale === "zh" ? item.zh : item.en}
                </button>
              ))}
            </div>
          </div>
        ) : (
          ordered.map((record) => (
            <TurnCard key={record.consultation_id} record={record}
              onStageDraft={readOnlyViewport ? undefined : onStageDraft} />
          ))
        )}
      </div>

      {(localError !== null || consultation.error !== null) && (
        <div className="collaboration-error" role="alert">
          <span>{localError !== null ? localErrorText(localError) : consultation.error}</span>
          <button
            type="button"
            aria-label={t("关闭会诊错误", "Dismiss consultation error")}
            onClick={() => {
              setLocalError(null);
              consultation.clearError();
            }}
          >
            ×
          </button>
        </div>
      )}

      <form
        className="consultation-composer"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <label htmlFor="consultation-question">{t("向协同宿主提问", "Ask the collaboration host")}</label>
        <textarea
          id="consultation-question"
          value={question}
          rows={3}
          maxLength={2000}
          placeholder={t("例如：当前可视范围内，回踩承接和突破确认哪个证据更充分？", "e.g. In the visible range, does pullback support or breakout confirmation have stronger evidence?")}
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
              event.preventDefault();
              void submit();
            }
          }}
        />
        <div className="composer-footer">
          <span className="composer-hint">
            {snapshot === null
              ? t("等待首个快照", "Waiting for the first snapshot")
              : t(`当前快照 ${snapshot.snapshot_id.slice(0, 8)}`, `Snapshot ${snapshot.snapshot_id.slice(0, 8)}`)}
          </span>
          <button type="submit" className="primary-button" disabled={disabled}>
            {busy ? t("冻结并请求…", "Freezing and requesting…") : t("请求协同会诊", "Request consultation")}
          </button>
        </div>
      </form>

      <footer className="collaboration-usage">
        <details>
          <summary>{t("Token 用量 · 宿主侧按回合幂等入账", "Token usage · idempotent per turn on the host")}</summary>
          <p data-testid="usage-line">{usageLine(consultation.usage, locale)}</p>
          <p className="usage-note">
            {t("缺失的用量标记为“未报”，不会按 0 计入。", "Missing usage is marked “unreported” and never counted as 0.")}
          </p>
        </details>
      </footer>
    </aside>
  );
}
