import { useState, type ReactNode } from "react";
import type { ScannerBriefing } from "@talaria/contracts";

import {
  abstainReasonLabel,
  briefingNeedsCollapse,
  briefingPreview,
  briefingStatusLabel,
  briefingStatusTone,
  briefingTime,
  briefingTitle,
} from "./model";
import type { BriefingsController } from "./useBriefings";

export interface BriefingInboxProps {
  controller: BriefingsController;
  open: boolean;
  onClose: () => void;
  onLocate: (briefing: ScannerBriefing) => void;
  onBringToCollab: (briefing: ScannerBriefing) => void;
  readOnly?: boolean;
  now?: number;
  tabBar?: ReactNode;
}

interface BriefingCardProps {
  briefing: ScannerBriefing;
  now: number;
  readOnly: boolean;
  onRead: (briefing: ScannerBriefing) => void;
  onLocate: (briefing: ScannerBriefing) => void;
  onBringToCollab: (briefing: ScannerBriefing) => void;
}

function BriefingCard({
  briefing,
  now,
  readOnly,
  onRead,
  onLocate,
  onBringToCollab,
}: BriefingCardProps) {
  const [expanded, setExpanded] = useState(false);
  const reason = briefing.summary?.trigger_reason ?? "";
  const needsCollapse = briefingNeedsCollapse(briefing);
  const unread = briefing.read_ts === null || briefing.read_ts === undefined;

  return (
    <article
      className={`briefing-card is-${briefingStatusTone(briefing.status)}`}
      data-testid="briefing-card"
      data-unread={unread ? "true" : "false"}
    >
      <div className="briefing-meta">
        <span className={`briefing-status is-${briefingStatusTone(briefing.status)}`}>
          {briefingStatusLabel(briefing.status)}
        </span>
        <time dateTime={new Date(briefing.created_ts).toISOString()}>
          {briefingTime(briefing.created_ts, now)}
        </time>
        {unread && (
          <span className="briefing-unread-dot" aria-label="未读">
            未读
          </span>
        )}
      </div>
      <h3 className="briefing-title">{briefingTitle(briefing)}</h3>
      <p className="briefing-symbol">
        {briefing.instrument_id} · {briefing.timeframe} · 快照{" "}
        {briefing.snapshot_id.slice(0, 8)}
      </p>

      {briefing.status === "READY" && (
        <div className="briefing-body">
          <p
            className={`briefing-reason${needsCollapse && !expanded ? " is-clamped" : ""}`}
          >
            {needsCollapse && !expanded ? briefingPreview(reason) : reason}
          </p>
          {needsCollapse && (
            <button
              type="button"
              className="briefing-expand"
              aria-expanded={expanded}
              onClick={() => setExpanded((value) => !value)}
            >
              {expanded ? "收起摘要" : "展开摘要"}
            </button>
          )}

          {(briefing.summary?.key_levels?.length ?? 0) > 0 && (
            <div className="briefing-levels">
              <span className="briefing-label">关键价位参考</span>
              <ul>
                {briefing.summary?.key_levels?.map((level, index) => (
                  <li key={`${level.label}-${index}`}>
                    <strong>{level.label}</strong>
                    {level.price != null && <span> {level.price}</span>}
                    {level.note != null && <em> · {level.note}</em>}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {(briefing.summary?.risks?.length ?? 0) > 0 && (
            <div className="briefing-risks">
              <span className="briefing-label">风险提示</span>
              <ul>
                {briefing.summary?.risks?.map((risk, index) => (
                  <li key={`${risk}-${index}`}>{risk}</li>
                ))}
              </ul>
            </div>
          )}

        </div>
      )}

      {briefing.status === "ABSTAIN" && (
        <div className="briefing-abstain" data-testid="briefing-abstain">
          <strong>Abstain</strong>
          <p>{abstainReasonLabel(briefing.abstain_reason)}</p>
        </div>
      )}

      {briefing.status === "FAILED" && (
        <div className="briefing-abstain" data-testid="briefing-failed">
          <strong>本轮未完成</strong>
          <p>宿主没有完成本轮简报，等待下一次扫描命中。</p>
        </div>
      )}

      {(briefing.status === "READY" || briefing.status === "ABSTAIN") &&
        (briefing.evidence_refs?.length ?? 0) > 0 && (
          <details className="briefing-evidence" data-testid="briefing-evidence">
            <summary>证据引用 · {briefing.evidence_refs?.length ?? 0}</summary>
            <ul>
              {briefing.evidence_refs?.map((evidence) => (
                <li key={evidence.evidence_id}>
                  <code>{evidence.evidence_id}</code>
                  <span> {evidence.label}</span>
                  {evidence.detail != null && <em> · {evidence.detail}</em>}
                </li>
              ))}
            </ul>
          </details>
        )}

      <div className="briefing-actions">
        <button
          type="button"
          className="ghost-button"
          onClick={() => {
            onRead(briefing);
            onLocate(briefing);
          }}
        >
          切到主图
        </button>
        {briefing.status === "READY" && !readOnly && (
          <button
            type="button"
            className="primary-button briefing-bring"
            onClick={() => {
              onRead(briefing);
              onBringToCollab(briefing);
            }}
          >
            带入协同
          </button>
        )}
        {unread && (
          <button
            type="button"
            className="ghost-button"
            onClick={() => onRead(briefing)}
          >
            标记已读
          </button>
        )}
      </div>
    </article>
  );
}

export function BriefingInbox({
  controller,
  open,
  onClose,
  onLocate,
  onBringToCollab,
  readOnly = false,
  now = Date.now(),
  tabBar,
}: BriefingInboxProps) {
  if (!open) return null;

  return (
    <aside className="workspace-copilot briefings-panel" data-testid="briefings-panel">
      {tabBar}
      <div className="briefings-head">
        <div>
          <h2 className="briefings-title">协同简报</h2>
          <p className="briefings-subtitle">
            扫描命中 · 冻结快照 · 零轮询模型
          </p>
        </div>
        <div className="briefings-head-actions">
          {controller.unread > 0 && (
            <span className="briefings-unread" aria-label="未读简报">
              {controller.unread} 条未读
            </span>
          )}
          <button
            type="button"
            className="ghost-button"
            aria-label="收起简报收件箱"
            onClick={onClose}
          >
            ×
          </button>
        </div>
      </div>

      {controller.error !== null && (
        <div className="briefings-error" role="alert" data-testid="briefings-error">
          <span>{controller.error}</span>
          <button type="button" aria-label="关闭简报错误" onClick={controller.clearError}>
            ×
          </button>
        </div>
      )}

      {controller.loading && controller.briefings.length === 0 && (
        <div className="briefings-loading" aria-live="polite">
          正在读取简报…
        </div>
      )}

      {!controller.loading && controller.briefings.length === 0 && (
        <div className="briefings-empty" data-testid="briefings-empty">
          <strong>暂无协同简报</strong>
          <p>扫描命中且通过冷却与全局令牌桶后，简报才会出现在这里。</p>
          <p>宿主离线或 --solo 时不生成简报，只保留纯数值异动卡。</p>
        </div>
      )}

      {controller.briefings.length > 0 && (
        <div className="briefings-list">
          {controller.briefings.map((briefing) => (
            <BriefingCard
              key={briefing.id}
              briefing={briefing}
              now={now}
              readOnly={readOnly}
              onRead={(item) => {
                if (item.read_ts === null || item.read_ts === undefined) {
                  void controller.markRead(item.id);
                }
              }}
              onLocate={onLocate}
              onBringToCollab={onBringToCollab}
            />
          ))}
        </div>
      )}
    </aside>
  );
}
