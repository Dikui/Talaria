import { useMemo, useState, type ReactNode } from "react";
import type { AgentAnnotationRecord } from "@talaria/contracts";

import { t, useLocale } from "../../lib/i18n";
import type { AnnotationsController } from "./useAnnotations";
import {
  annotationBadge,
  annotationPriceLabel,
  annotationStatusLabel,
  canManageAnnotations,
  evidenceLines,
  filterAnnotations,
  kindLabel,
} from "./model";

export interface AnnotationPanelProps {
  controller: AnnotationsController;
  instrumentId: string;
  snapshotId: string | null;
  readOnly: boolean;
  tabBar?: ReactNode;
  onClose: () => void;
  onAdopted: (drawingId: string) => void;
  onError: (message: string) => void;
}

/** 右栏「智能体标注」：审计流水、按会话过滤与批量清理；采纳/忽略需人工点击。 */
export function AnnotationPanel({
  controller,
  instrumentId,
  snapshotId,
  readOnly,
  tabBar,
  onClose,
  onAdopted,
  onError,
}: AnnotationPanelProps) {
  const locale = useLocale();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [clearing, setClearing] = useState(false);

  const sessions = useMemo(
    () =>
      controller.sessions.filter((session) =>
        session.instrument_ids.includes(instrumentId),
      ),
    [controller.sessions, instrumentId],
  );
  const visible = useMemo(
    () => filterAnnotations(controller.annotations, sessionId),
    [controller.annotations, sessionId],
  );
  const active = visible.filter((annotation) => annotation.status === "ACTIVE");
  const manageable = canManageAnnotations(readOnly);

  const adopt = async (annotation: AgentAnnotationRecord) => {
    setBusyId(annotation.annotation_id);
    const result = await controller.adopt(annotation.annotation_id);
    setBusyId(null);
    if (result === null) {
      onError(controller.error ?? t("采纳标注失败", "Failed to adopt annotation"));
      return;
    }
    if (result.adopted_drawing_id !== null && result.adopted_drawing_id !== undefined) {
      onAdopted(result.adopted_drawing_id);
    }
  };

  const dismiss = async (annotation: AgentAnnotationRecord) => {
    setBusyId(annotation.annotation_id);
    const result = await controller.dismiss(annotation.annotation_id);
    setBusyId(null);
    if (result === null) onError(controller.error ?? t("忽略标注失败", "Failed to dismiss annotation"));
  };

  const clear = async () => {
    const scope = sessionId === null
      ? t("当前标的全部", "all instruments in this chart")
      : t(`会话 ${sessionId}`, `session ${sessionId}`);
    if (!window.confirm(t(
      `清理${scope}的活跃智能体标注？审计记录会保留。`,
      `Clear active agent annotations for ${scope}? The audit log is retained.`,
    ))) return;
    setClearing(true);
    const cleared = await controller.clear(sessionId ?? undefined);
    setClearing(false);
    if (cleared === null) onError(controller.error ?? t("清理标注失败", "Failed to clear annotations"));
  };

  return (
    <aside className="workspace-copilot annotation-panel" data-testid="annotation-panel" aria-label={t("智能体标注", "Agent annotations")}>
      {tabBar}
      <header className="annotation-panel-head">
        <div>
          <h2>{t("智能体标注", "Agent annotations")}</h2>
          <p className="annotation-panel-sub">
            {t(
              `${annotationBadge(locale)}虚线建议 · ${active.length} 条活跃 · 上限 12`,
              `${annotationBadge(locale)} dashed suggestions · ${active.length} active · limit 12`,
            )}
          </p>
        </div>
        <button type="button" className="right-dock-close" aria-label={t("关闭标注面板", "Close annotations panel")} onClick={onClose}>
          ×
        </button>
      </header>

      <div className="annotation-panel-toolbar">
        <label>
          {t("会话", "Session")}
          <select
            value={sessionId ?? ""}
            onChange={(event) => setSessionId(event.target.value === "" ? null : event.target.value)}
            data-testid="annotation-session-filter"
          >
            <option value="">{t("全部会话", "All sessions")}</option>
            {sessions.map((session) => (
              <option key={session.session_id} value={session.session_id}>
                {t(`${session.session_id}（${session.annotation_count}）`, `${session.session_id} (${session.annotation_count})`)}
              </option>
            ))}
          </select>
        </label>
        {manageable && (
          <button
            type="button"
            className="ghost-button"
            data-testid="annotation-clear"
            disabled={clearing || active.length === 0}
            onClick={() => void clear()}
          >
            {clearing ? t("清理中…", "Clearing…") : t("批量清理", "Clear all")}
          </button>
        )}
      </div>

      {controller.error !== null && (
        <div className="annotation-error" role="alert" data-testid="annotation-error">
          <span>{controller.error}</span>
          <button type="button" aria-label={t("关闭标注错误", "Dismiss annotation error")} onClick={controller.clearError}>
            ×
          </button>
        </div>
      )}

      {controller.loading ? (
        <p className="annotation-empty">{t("正在读取标注…", "Loading annotations…")}</p>
      ) : visible.length === 0 ? (
        <div className="annotation-empty" data-testid="annotation-empty">
          <strong>{t("还没有智能体标注", "No agent annotations yet")}</strong>
          <p>
            {t("在 DSH 中让分析副驾驶调用", "Ask the analysis copilot in DSH to call")}{" "}
            <code>write_agent_overlay</code>
            {t("，通过快照门控的虚线建议会出现在这里。", "; snapshot-gated dashed suggestions will appear here.")}
          </p>
        </div>
      ) : (
        <ul className="annotation-list">
          {visible.map((annotation) => {
            const canAct = manageable && annotation.status === "ACTIVE";
            return (
              <li
                key={annotation.annotation_id}
                className={`annotation-card annotation-card--${annotation.status.toLowerCase()}`}
                data-testid={`annotation-card-${annotation.annotation_id}`}
              >
                <div className="annotation-card-head">
                  <strong>{annotation.label}</strong>
                  <span className={`annotation-status annotation-status--${annotation.status.toLowerCase()}`}>
                    {annotationStatusLabel(annotation.status, locale)}
                  </span>
                </div>
                <p className="annotation-card-price">
                  {kindLabel(annotation.kind, locale)} · {annotationPriceLabel(annotation)}
                </p>
                <p className="annotation-card-meta">
                  {t(`快照 ${annotation.snapshot_id.slice(0, 8)} · 第 ${annotation.round} 轮`, `Snapshot ${annotation.snapshot_id.slice(0, 8)} · Round ${annotation.round}`)} · {annotation.session_id}
                </p>
                {annotation.rationale.length > 0 && (
                  <p className="annotation-card-rationale">{annotation.rationale}</p>
                )}
                <details className="annotation-card-evidence">
                  <summary>{t(`证据 ${evidenceLines(annotation, locale).length} 条`, `${evidenceLines(annotation, locale).length} evidence items`)}</summary>
                  <ol>
                    {evidenceLines(annotation, locale).map((line, index) => (
                      <li key={`${index}-${line}`}>{line}</li>
                    ))}
                  </ol>
                </details>
                {annotation.archived_reason !== null && annotation.archived_reason !== undefined && (
                  <p className="annotation-card-archived">{annotation.archived_reason}</p>
                )}
                {annotation.adopted_drawing_id !== null && annotation.adopted_drawing_id !== undefined && (
                  <p className="annotation-card-adopted">{t("已生成人工画线", "Drawing created")} {annotation.adopted_drawing_id}</p>
                )}
                {canAct && (
                  <div className="annotation-card-actions">
                    <button
                      type="button"
                      className="primary-button"
                      disabled={busyId === annotation.annotation_id}
                      onClick={() => void adopt(annotation)}
                    >
                      {busyId === annotation.annotation_id ? t("处理中…", "Working…") : t("采纳为画线", "Adopt as drawing")}
                    </button>
                    <button
                      type="button"
                      className="ghost-button"
                      disabled={busyId === annotation.annotation_id}
                      onClick={() => void dismiss(annotation)}
                    >
                      {t("忽略", "Dismiss")}
                    </button>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {snapshotId !== null && (
        <footer className="annotation-panel-foot">
          {t(`当前快照 ${snapshotId.slice(0, 8)}；采纳会复制为独立 HUMAN_DRAWING，忽略只改标注状态。`, `Snapshot ${snapshotId.slice(0, 8)}; adopting copies it into a standalone HUMAN_DRAWING, whereas dismissing only changes the annotation status.`)}
        </footer>
      )}
    </aside>
  );
}
