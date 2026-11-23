import { t, useLocale } from "../../lib/i18n";
import { drawingKindLabel } from "./tools";
import type { DrawingsController } from "./useDrawings";

export interface DrawingManagerProps {
  controller: DrawingsController;
  onClose(): void;
}

export function DrawingManager({ controller, onClose }: DrawingManagerProps) {
  const { drawings, deleteDrawing, loading } = controller;
  const locale = useLocale();
  return (
    <aside className="drawing-manager" data-testid="drawing-manager-panel">
      <header className="drawing-manager-header">
        <strong>{t("画线管理", "Drawing manager")}</strong>
        <span className="drawing-manager-count" data-testid="drawing-manager-count">
          {t(`${drawings.length} 条`, `${drawings.length} ${drawings.length === 1 ? "drawing" : "drawings"}`)}
        </span>
        <button
          type="button"
          className="drawing-inspector-close"
          aria-label={t("关闭画线管理", "Close drawing manager")}
          onClick={onClose}
        >
          ×
        </button>
      </header>
      {loading && drawings.length === 0 ? (
        <p className="drawing-manager-empty">{t("画线加载中…", "Loading drawings…")}</p>
      ) : drawings.length === 0 ? (
        <p className="drawing-manager-empty" data-testid="drawing-manager-empty">
          {t("当前窗口暂无画线，使用上方工具栏开始绘制。", "No drawings in this window yet. Use the toolbar above to start drawing.")}
        </p>
      ) : (
        <ul className="drawing-manager-list">
          {drawings.map((drawing) => (
            <li key={drawing.drawing_id} data-testid={`drawing-manager-item-${drawing.drawing_id}`}>
              <button
                type="button"
                className="drawing-manager-select"
                onClick={() => controller.select(drawing.drawing_id)}
              >
                <span className="drawing-manager-kind">{drawingKindLabel(drawing.kind, locale)}</span>
                <span className="drawing-manager-meta">
                  {t(`v${drawing.version} · ${drawing.points.length} 点`, `v${drawing.version} · ${drawing.points.length} points`)}
                  {drawing.text.length > 0 ? ` · ${drawing.text}` : ""}
                </span>
              </button>
              <button
                type="button"
                className="drawing-manager-delete"
                data-testid={`drawing-manager-delete-${drawing.drawing_id}`}
                aria-label={t(`删除 ${drawingKindLabel(drawing.kind, locale)}`, `Delete ${drawingKindLabel(drawing.kind, locale)}`)}
                onClick={() => deleteDrawing(drawing.drawing_id)}
              >
                {t("删除", "Delete")}
              </button>
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}
