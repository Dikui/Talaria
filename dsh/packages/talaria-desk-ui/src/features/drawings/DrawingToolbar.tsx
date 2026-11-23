import { t, useLocale } from "../../lib/i18n";
import type { DrawingsController } from "./useDrawings";
import { DRAWING_TOOLS, drawingToolHint, drawingToolLabel } from "./tools";

export interface DrawingToolbarProps {
  controller: DrawingsController;
  managerOpen: boolean;
  onToggleManager(): void;
}

export function DrawingToolbar({
  controller,
  managerOpen,
  onToggleManager,
}: DrawingToolbarProps) {
  const {
    activeTool,
    setTool,
    canUndo,
    canRedo,
    undo,
    redo,
    clearInstrument,
    pending,
    loading,
    toolHint,
    readOnly,
  } = controller;
  const locale = useLocale();
  if (readOnly) return null;
  const busy = pending > 0;
  return (
    <div className="drawing-toolbar" data-testid="drawing-toolbar">
      <div className="drawing-tools" role="toolbar" aria-label={t("画线工具", "Drawing tools")}>
        {DRAWING_TOOLS.map((tool) => (
          <button
            key={tool.id}
            type="button"
            className={`drawing-tool${activeTool === tool.id ? " is-active" : ""}`}
            data-testid={`drawing-tool-${tool.id}`}
            title={drawingToolHint(tool, locale)}
            aria-pressed={activeTool === tool.id}
            onClick={() => setTool(activeTool === tool.id ? "select" : tool.id)}
          >
            {drawingToolLabel(tool, locale)}
          </button>
        ))}
      </div>
      <div className="drawing-actions">
        <button
          type="button"
          className="drawing-action"
          data-testid="drawing-undo"
          disabled={!canUndo}
          onClick={undo}
        >
          {t("撤销", "Undo")}
        </button>
        <button
          type="button"
          className="drawing-action"
          data-testid="drawing-redo"
          disabled={!canRedo}
          onClick={redo}
        >
          {t("重做", "Redo")}
        </button>
        <button
          type="button"
          className={`drawing-action${managerOpen ? " is-active" : ""}`}
          data-testid="drawing-manager"
          onClick={onToggleManager}
        >
          {t("管理", "Manage")}
        </button>
        <button
          type="button"
          className="drawing-action"
          data-testid="drawing-clear"
          onClick={clearInstrument}
        >
          {t("清空标的", "Clear instrument")}
        </button>
      </div>
      <span className="drawing-hint" data-testid="drawing-hint">
        {loading ? t("画线加载中…", "Loading drawings…") : busy ? t("同步中…", "Syncing…") : toolHint}
      </span>
    </div>
  );
}
