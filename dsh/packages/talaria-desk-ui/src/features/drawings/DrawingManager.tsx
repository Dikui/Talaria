import { KIND_LABELS } from "./tools";
import type { DrawingsController } from "./useDrawings";

export interface DrawingManagerProps {
  controller: DrawingsController;
  onClose(): void;
}

export function DrawingManager({ controller, onClose }: DrawingManagerProps) {
  const { drawings, deleteDrawing, loading } = controller;
  return (
    <aside className="drawing-manager" data-testid="drawing-manager-panel">
      <header className="drawing-manager-header">
        <strong>画线管理</strong>
        <span className="drawing-manager-count" data-testid="drawing-manager-count">
          {drawings.length} 条
        </span>
        <button
          type="button"
          className="drawing-inspector-close"
          aria-label="关闭画线管理"
          onClick={onClose}
        >
          ×
        </button>
      </header>
      {loading && drawings.length === 0 ? (
        <p className="drawing-manager-empty">画线加载中…</p>
      ) : drawings.length === 0 ? (
        <p className="drawing-manager-empty" data-testid="drawing-manager-empty">
          当前窗口暂无画线，使用上方工具栏开始绘制。
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
                <span className="drawing-manager-kind">{KIND_LABELS[drawing.kind]}</span>
                <span className="drawing-manager-meta">
                  v{drawing.version} · {drawing.points.length} 点
                  {drawing.text.length > 0 ? ` · ${drawing.text}` : ""}
                </span>
              </button>
              <button
                type="button"
                className="drawing-manager-delete"
                data-testid={`drawing-manager-delete-${drawing.drawing_id}`}
                aria-label={`删除 ${KIND_LABELS[drawing.kind]}`}
                onClick={() => deleteDrawing(drawing.drawing_id)}
              >
                删除
              </button>
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}
