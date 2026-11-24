import { t, useLocale } from "../../lib/i18n";
import {
  annotationPriceLabel,
  annotationStatusLabel,
  evidenceLines,
  kindLabel,
} from "./model";
import type { AnnotationsController } from "./useAnnotations";

export function AnnotationPanel({
  controller,
  readOnly,
  onAdopted,
}: {
  controller: AnnotationsController;
  readOnly: boolean;
  onAdopted?: () => void;
}) {
  const locale = useLocale();
  const { annotations, loading, error, adopt, dismiss, refresh } = controller;

  return (
    <section
      className="dock-panel annotation-panel"
      data-testid="annotation-panel"
      aria-label={t("智能体只读标注", "Agent read-only annotations")}
    >
      <header className="dock-panel-head">
        <div>
          <h2>{t("智能体标注 (只读)", "Agent Annotations (Read-only)")}</h2>
          <span className="dock-panel-meta">{annotations.length}</span>
        </div>
        <button type="button" className="ghost-button" onClick={() => void refresh()}>
          {t("刷新", "Refresh")}
        </button>
      </header>

      {error && (
        <p className="panel-error" role="alert">
          {error}
        </p>
      )}

      {loading && annotations.length === 0 ? (
        <p className="panel-empty">{t("加载标注中…", "Loading annotations…")}</p>
      ) : annotations.length === 0 ? (
        <p className="panel-empty">{t("暂无外部智能体标注。", "No external agent annotations.")}</p>
      ) : (
        <ul className="entity-list">
          {annotations.map((item) => (
            <li key={item.annotation_id} className="entity-card">
              <div className="entity-card-top">
                <span className="entity-tag">{kindLabel(item.kind, locale)}</span>
                <span className={`status-pill status-${item.status.toLowerCase()}`}>
                  {annotationStatusLabel(item.status, locale)}
                </span>
              </div>
              <div className="entity-card-main">
                <strong>
                  {item.label} · {annotationPriceLabel(item)}
                </strong>
                {item.rationale && <p className="entity-rationale">{item.rationale}</p>}
                {evidenceLines(item, locale).map((line, i) => (
                  <span key={i} className="entity-evidence">
                    {line}
                  </span>
                ))}
              </div>
              {!readOnly && item.status === "ACTIVE" && (
                <div className="entity-actions">
                  <button
                    type="button"
                    className="ghost-button"
                    onClick={() => void adopt(item.annotation_id).then(() => onAdopted?.())}
                  >
                    {t("采纳为画线", "Adopt")}
                  </button>
                  <button
                    type="button"
                    className="ghost-button"
                    onClick={() => void dismiss(item.annotation_id)}
                  >
                    {t("忽略", "Dismiss")}
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
