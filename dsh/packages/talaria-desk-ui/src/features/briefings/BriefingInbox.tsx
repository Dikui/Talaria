import { useState, type ReactNode } from "react";

import { t, useLocale } from "../../lib/i18n";
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
  const locale = useLocale();
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
          {briefingStatusLabel(briefing.status, locale)}
        </span>
        <time dateTime={new Date(briefing.created_ts).toISOString()}>
          {briefingTime(briefing.created_ts, now, locale)}
        </time>
        {unread && (
          <span className="briefing-unread-dot" aria-label={t("未读", "Unread")}>
            {t("未读", "Unread")}
          </span>
        )}
      </div>
      <h3 className="briefing-title">{briefingTitle(briefing, locale)}</h3>
      <p className="briefing-symbol">
        {briefing.instrument_id} · {briefing.timeframe} · {t("快照", "Snapshot")}{" "}
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
              {expanded ? t("收起摘要", "Collapse summary") : t("展开摘要", "Expand summary")}
            </button>
          )}

          {(briefing.summary?.key_levels?.length ?? 0) > 0 && (
            <div className="briefing-levels">
              <span className="briefing-label">{t("关键价位参考", "Key levels")}</span>
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
              <span className="briefing-label">{t("风险提示", "Risks")}</span>
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
          <p>{abstainReasonLabel(briefing.abstain_reason, locale)}</p>
        </div>
      )}

      {briefing.status === "FAILED" && (
        <div className="briefing-abstain" data-testid="briefing-failed">
          <strong>{t("本轮未完成", "This round did not finish")}</strong>
          <p>{t("宿主没有完成本轮简报，等待下一次扫描命中。", "The host did not finish this briefing; waiting for the next scan hit.")}</p>
        </div>
      )}

      {(briefing.status === "READY" || briefing.status === "ABSTAIN") &&
        (briefing.evidence_refs?.length ?? 0) > 0 && (
          <details className="briefing-evidence" data-testid="briefing-evidence">
            <summary>{t("证据引用", "Evidence")} · {briefing.evidence_refs?.length ?? 0}</summary>
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
          {t("切到主图", "Open in chart")}
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
            {t("带入协同", "Bring to consultation")}
          </button>
        )}
        {unread && (
          <button
            type="button"
            className="ghost-button"
            onClick={() => onRead(briefing)}
          >
            {t("标记已读", "Mark as read")}
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
  useLocale();
  if (!open) return null;

  return (
    <aside className="workspace-copilot briefings-panel" data-testid="briefings-panel">
      {tabBar}
      <div className="briefings-head">
        <div>
          <h2 className="briefings-title">{t("协同简报", "Collaborative briefings")}</h2>
          <p className="briefings-subtitle">
            {t("扫描命中 · 冻结快照 · 零轮询模型", "Scan hit · frozen snapshot · no-model polling")}
          </p>
        </div>
        <div className="briefings-head-actions">
          {controller.unread > 0 && (
            <span className="briefings-unread" aria-label={t("未读简报", "Unread briefings")}>
              {t(`${controller.unread} 条未读`, `${controller.unread} unread`)}
            </span>
          )}
          <button
            type="button"
            className="ghost-button"
            aria-label={t("收起简报收件箱", "Collapse briefing inbox")}
            onClick={onClose}
          >
            ×
          </button>
        </div>
      </div>

      {controller.error !== null && (
        <div className="briefings-error" role="alert" data-testid="briefings-error">
          <span>{controller.error}</span>
          <button type="button" aria-label={t("关闭简报错误", "Dismiss briefing error")} onClick={controller.clearError}>
            ×
          </button>
        </div>
      )}

      {controller.loading && controller.briefings.length === 0 && (
        <div className="briefings-loading" aria-live="polite">
          {t("正在读取简报…", "Loading briefings…")}
        </div>
      )}

      {!controller.loading && controller.briefings.length === 0 && (
        <div className="briefings-empty" data-testid="briefings-empty">
          <strong>{t("暂无协同简报", "No collaborative briefings yet")}</strong>
          <p>{t("扫描命中且通过冷却与全局令牌桶后，简报才会出现在这里。", "Briefings appear here after a scan hit passes the cooldown and the global token bucket.")}</p>
          <p>{t("宿主离线或 --solo 时不生成简报，只保留纯数值异动卡。", "When the host is offline or in --solo mode, no briefing is generated; only the numeric move card is kept.")}</p>
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
