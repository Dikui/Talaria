import type { DrawingsController } from "./useDrawings";
import { DRAWING_TOOLS } from "./tools";

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
  if (readOnly) return null;
  const busy = pending > 0;
  return (
    <div className="drawing-toolbar" data-testid="drawing-toolbar">
      <div className="drawing-tools" role="toolbar" aria-label="画线工具">
        {DRAWING_TOOLS.map((tool) => (
          <button
            key={tool.id}
            type="button"
            className={`drawing-tool${activeTool === tool.id ? " is-active" : ""}`}
            data-testid={`drawing-tool-${tool.id}`}
            title={tool.hint}
            aria-pressed={activeTool === tool.id}
            onClick={() => setTool(activeTool === tool.id ? "select" : tool.id)}
          >
            {tool.label}
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
          撤销
        </button>
        <button
          type="button"
          className="drawing-action"
          data-testid="drawing-redo"
          disabled={!canRedo}
          onClick={redo}
        >
          重做
        </button>
        <button
          type="button"
          className={`drawing-action${managerOpen ? " is-active" : ""}`}
          data-testid="drawing-manager"
          onClick={onToggleManager}
        >
          管理
        </button>
        <button
          type="button"
          className="drawing-action"
          data-testid="drawing-clear"
          onClick={clearInstrument}
        >
          清空标的
        </button>
      </div>
      <span className="drawing-hint" data-testid="drawing-hint">
        {loading ? "画线加载中…" : busy ? "同步中…" : toolHint}
      </span>
    </div>
  );
}
