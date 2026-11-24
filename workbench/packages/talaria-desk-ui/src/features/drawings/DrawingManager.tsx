import type { DrawingRecord } from "@talaria/contracts";

import { t, useLocale } from "../../lib/i18n";
import { drawingKindLabel } from "./tools";

export interface DrawingManagerProps {
  drawings: DrawingRecord[];
  selectedId: string | null;
  readOnly: boolean;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onAttachAlert: (drawing: DrawingRecord) => void;
  onClearInstrument?: () => void;
}

export function DrawingManager({
  drawings,
  selectedId,
  readOnly,
  onSelect,
  onDelete,
  onAttachAlert,
  onClearInstrument,
}: DrawingManagerProps) {
  const locale = useLocale();
  const activeDrawings = drawings.filter((d) => d.deleted_at == null);

  return (
    <section
      className="dock-panel drawing-manager"
      data-testid="drawing-manager-panel"
      aria-label={t("画线管理器", "Drawing manager")}
    >
      <header className="dock-panel-head">
        <div>
          <h2>{t("画线管理", "Drawings")}</h2>
          <span className="dock-panel-meta" data-testid="drawing-manager-count">
            {activeDrawings.length}
          </span>
        </div>
        {!readOnly && onClearInstrument && activeDrawings.length > 0 && (
          <button
            type="button"
            className="ghost-button"
            onClick={onClearInstrument}
          >
            {t("清空", "Clear")}
          </button>
        )}
      </header>

      {readOnly && (
        <p className="panel-note">
          {t("窄屏只读 · 仅展示已有画线", "Read-only viewport · Drawings displayed only")}
        </p>
      )}

      {activeDrawings.length === 0 ? (
        <p className="panel-empty" data-testid="drawing-manager-empty">
          {t("当前标的暂无持久化画线。", "No persisted drawings for this symbol.")}
        </p>
      ) : (
        <ul className="entity-list">
          {activeDrawings.map((drawing) => (
            <li
              key={drawing.drawing_id}
              className={`entity-card${selectedId === drawing.drawing_id ? " is-highlighted" : ""}`}
              data-testid={`drawing-manager-item-${drawing.drawing_id}`}
            >
              <button
                type="button"
                className="drawing-select-button"
                onClick={() => onSelect(drawing.drawing_id)}
              >
                <strong>{drawingKindLabel(drawing.kind, locale)}</strong>
                <span>
                  v{drawing.version} · {drawing.points.length}{" "}
                  {t("锚点", "pts")}
                  {drawing.text ? ` · ${drawing.text}` : ""}
                </span>
              </button>
              {!readOnly && (
                <div className="entity-actions">
                  <button
                    type="button"
                    className="ghost-button"
                    onClick={() => onAttachAlert(drawing)}
                  >
                    {t("设预警", "Alert")}
                  </button>
                  <button
                    type="button"
                    className="ghost-button"
                    data-testid={`drawing-manager-delete-${drawing.drawing_id}`}
                    onClick={() => onDelete(drawing.drawing_id)}
                  >
                    {t("删除", "Delete")}
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
