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
  "现在更适合等回踩，还是等突破确认？",
  "框选区间内有哪些关键位值得关注？",
  "如果入场，止损和目标应该放在哪里？",
];

const timestampFormatter = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

function formatTs(ms: number): string {
  return timestampFormatter.format(new Date(ms));
}

function formatRange(window: CollabWindow): string {
  return `${formatTs(window.from_ts)} → ${formatTs(window.to_ts)} · ${window.price_min}–${window.price_max}`;
}

function EvidenceFold({ refs }: { refs: readonly EvidenceRef[] | undefined }) {
  const groups = useMemo(() => groupEvidence(refs ?? []), [refs]);
  const count = refs?.length ?? 0;
  if (count === 0) return null;
  return (
    <details className="evidence-fold" data-testid="evidence-fold">
      <summary>
        证据 {count} 条 · 展开核对
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
  return (
    <div className="insight-card" data-testid="insight-card">
      <div className="insight-head">
        <span className={`direction-badge direction-badge--${insight.direction.toLowerCase()}`}>
          {directionLabel(insight.direction)}
        </span>
        <span className="insight-note">观察建议 · 非订单</span>
      </div>
      <p className="insight-summary">{insight.summary}</p>
      <dl className="insight-levels">
        <div>
          <dt>入场带</dt>
          <dd>
            {insight.entry_low}–{insight.entry_high}
          </dd>
        </div>
        <div>
          <dt>证伪止损</dt>
          <dd>{insight.invalidation_stop}</dd>
        </div>
        <div>
          <dt>止盈目标</dt>
          <dd>{insight.take_profit.join(" / ")}</dd>
        </div>
        {insight.max_position !== null && insight.max_position !== undefined && (
          <div>
            <dt>持仓上限</dt>
            <dd>{insight.max_position}</dd>
          </div>
        )}
        {insight.valid_until !== null && insight.valid_until !== undefined && (
          <div>
            <dt>有效期至</dt>
            <dd>{formatTs(insight.valid_until)}</dd>
          </div>
        )}
      </dl>
      <EvidenceFold refs={insight.evidence_refs} />
    </div>
  );
}

function TurnCard({ record }: { record: ConsultationRecord }) {
  const abstain = record.abstain;
  return (
    <article
      className={`consultation-turn consultation-turn--${record.status.toLowerCase()}`}
      data-testid="consultation-turn"
    >
      <div className="consultation-question">
        <span className="question-mark">问</span>
        <p>{record.package.question}</p>
        <time dateTime={new Date(record.created_ts).toISOString()}>
          {formatTs(record.created_ts)}
        </time>
      </div>
      <div className="consultation-answer">
        <span className="consultation-status">{consultationStatusLabel(record)}</span>
        {record.status === "PENDING" && (
          <div className="consultation-pending" role="status">
            <span className="pending-dots" aria-hidden="true">
              <i />
              <i />
              <i />
            </span>
            协同宿主正在核对只读工具结果；图表与画线不受影响。
          </div>
        )}
        {record.status === "READY" && record.insight !== null && record.insight !== undefined && (
          <InsightCard insight={record.insight} />
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
              <strong>{abstainCopy(abstain).title}</strong>
              <code>{abstain.reason_code}</code>
            </div>
            <p>{abstain.message}</p>
            <p className="abstain-hint">{abstainCopy(abstain).hint}</p>
            <EvidenceFold refs={abstain.evidence_refs} />
          </div>
        )}
        {record.status === "FAILED" && (
          <div className="consultation-failed" role="alert">
            {record.error ?? "协同会诊失败；本地图表不受影响。"}
          </div>
        )}
        {record.dsh_session_id !== null && record.dsh_session_id !== undefined && (
          <p className="consultation-binding">
            宿主回合 {record.dsh_session_id.slice(0, 12)} · turn{" "}
            {record.dsh_turn_id ?? "—"}
          </p>
        )}
      </div>
    </article>
  );
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
  tabBar?: ReactNode;
  onClose: () => void;
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
  tabBar,
  onClose,
}: CollaborationPanelProps) {
  const consultation = useConsultation();
  const [question, setQuestion] = useState("");
  const [freezing, setFreezing] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

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
      setLocalError("图表尚未就绪，无法冻结可视范围；请稍后再试。");
      return;
    }
    setLocalError(null);
    setFreezing(true);
    try {
      const frozen = await freezeSnapshot();
      if (frozen === null) {
        setLocalError("快照冻结失败；图表与画线仍可继续操作。");
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
      setLocalError(caught instanceof Error ? caught.message : "会诊请求未发出");
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
      aria-label="协同会诊"
    >
      {tabBar}
      <header className="collaboration-head">
        <div>
          <h2 className="collaboration-title">协同会诊</h2>
          <p className="collaboration-subtitle">
            冻结快照 · 只读工具 · Setup Insight
          </p>
        </div>
        <span
          className={`host-badge host-badge--${hostView.tone}`}
          data-testid="collab-host-status"
          title={hostView.status?.message ?? "正在检测协同宿主"}
        >
          {hostView.label}
        </span>
      </header>

      {hostView.tone === "warning" && (
        <div className="collaboration-host-warning" role="status">
          协同宿主忙碌或离线：图表、画线、预警与快照冻结仍然可用；会诊将快速
          Abstain 或稍后重试。
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
            {selecting ? "正在框选…" : "框选区间"}
          </button>
        )}
        {selection === null ? (
          <span className="selection-empty">未框选：使用整个可视范围</span>
        ) : (
          <>
            <span className="selection-summary" data-testid="selection-summary">
              {formatRange(selection)}
            </span>
            <button type="button" className="link-button" onClick={onClearSelection}>
              清除
            </button>
          </>
        )}
      </div>

      <div className="collaboration-flow" ref={listRef} data-testid="consultation-flow">
        {consultation.loading ? (
          <p className="collaboration-loading">正在读取最近会诊…</p>
        ) : consultation.records.length === 0 ? (
          <div className="collaboration-empty" data-testid="collaboration-empty">
            <div className="collaboration-empty-mark" aria-hidden="true">
              ◎
            </div>
            <h3>还没有会诊记录</h3>
            <p>
              冻结当前快照后，宿主会调用只读工具核对指标、关键位与画线，再给出
              带证据的观察建议；证据不足时会明确 Abstain。
            </p>
            <div className="collaboration-examples">
              {EXAMPLES.map((item) => (
                <button
                  key={item}
                  type="button"
                  className="example-chip"
                  onClick={() => setQuestion(item)}
                >
                  {item}
                </button>
              ))}
            </div>
          </div>
        ) : (
          ordered.map((record) => (
            <TurnCard key={record.consultation_id} record={record} />
          ))
        )}
      </div>

      {(localError !== null || consultation.error !== null) && (
        <div className="collaboration-error" role="alert">
          <span>{localError ?? consultation.error}</span>
          <button
            type="button"
            aria-label="关闭会诊错误"
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
        <label htmlFor="consultation-question">向协同宿主提问</label>
        <textarea
          id="consultation-question"
          value={question}
          rows={3}
          maxLength={2000}
          placeholder="例如：当前可视范围内，回踩承接和突破确认哪个证据更充分？"
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
              ? "等待首个快照"
              : `当前快照 ${snapshot.snapshot_id.slice(0, 8)}`}
          </span>
          <button type="submit" className="primary-button" disabled={disabled}>
            {busy ? "冻结并请求…" : "请求协同会诊"}
          </button>
        </div>
      </form>

      <footer className="collaboration-usage">
        <details>
          <summary>Token 用量 · 宿主侧按回合幂等入账</summary>
          <p data-testid="usage-line">{usageLine(consultation.usage)}</p>
          <p className="usage-note">
            缺失的用量标记为“未报”，不会按 0 计入。
          </p>
        </details>
      </footer>
    </aside>
  );
}
