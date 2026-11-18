import { useEffect, useState } from "react";
import type { DrawingRecord } from "@talaria/contracts";

import { api } from "../../lib/api";
import { DRAWING_COLORS, KIND_LABELS } from "./tools";
import type { DrawingsController } from "./useDrawings";

export interface DrawingInspectorProps {
  controller: DrawingsController;
  timeframe: string;
}

const WIDTHS = [1, 2, 3] as const;

export function DrawingInspector({ controller, timeframe }: DrawingInspectorProps) {
  const { selected, updateSelected, deleteDrawing } = controller;
  const [text, setText] = useState(selected?.text ?? "");
  const [showVersions, setShowVersions] = useState(false);
  const [versions, setVersions] = useState<DrawingRecord[] | null>(null);
  const [versionsError, setVersionsError] = useState<string | null>(null);

  useEffect(() => {
    setText(selected?.text ?? "");
    setShowVersions(false);
    setVersions(null);
    setVersionsError(null);
  }, [selected?.drawing_id, selected?.version, selected?.text]);

  if (selected == null) return null;
  const currentTimeframes = selected.visible_timeframes;
  const allTimeframes = currentTimeframes.length === 0;
  const onlyCurrent = !allTimeframes && currentTimeframes.length === 1 && currentTimeframes[0] === timeframe;

  const commitText = () => {
    const trimmed = text.trim();
    const next =
      trimmed.length > 0 ? trimmed : selected.kind === "TEXT_LABEL" ? "标注" : "";
    if (next !== selected.text) {
      updateSelected({ text: next });
    }
    setText(next);
  };

  const toggleVersions = () => {
    const next = !showVersions;
    setShowVersions(next);
    if (!next || versions != null) return;
    setVersionsError(null);
    void api
      .drawingVersions(selected.drawing_id)
      .then((response) => setVersions(response.versions))
      .catch((caught: unknown) => {
        setVersionsError(caught instanceof Error ? caught.message : "版本读取失败");
      });
  };

  return (
    <aside className="drawing-inspector" data-testid="drawing-inspector">
      <header className="drawing-inspector-header">
        <strong>{KIND_LABELS[selected.kind]}</strong>
        <span className="drawing-version" data-testid="drawing-version">
          v{selected.version}
        </span>
        <button
          type="button"
          className="drawing-inspector-close"
          aria-label="关闭属性面板"
          onClick={() => controller.select(null)}
        >
          ×
        </button>
      </header>

      <label className="drawing-field">
        <span>文本</span>
        <input
          data-testid="drawing-text-input"
          value={text}
          maxLength={500}
          placeholder={selected.kind === "TEXT_LABEL" ? "标注文本" : "备注（可选）"}
          onChange={(event) => setText(event.target.value)}
          onBlur={commitText}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.currentTarget.blur();
            }
            if (event.key === "Escape") {
              setText(selected.text);
              event.currentTarget.blur();
            }
          }}
        />
      </label>

      <div className="drawing-field">
        <span>颜色</span>
        <div className="drawing-swatches">
          {DRAWING_COLORS.map((color) => (
            <button
              key={color}
              type="button"
              className={`drawing-swatch${selected.style.color === color ? " is-active" : ""}`}
              data-testid={`drawing-color-${color.replace("#", "")}`}
              style={{ backgroundColor: color }}
              aria-label={`颜色 ${color}`}
              onClick={() =>
                updateSelected({ style: { ...selected.style, color } })
              }
            />
          ))}
        </div>
      </div>

      <div className="drawing-field">
        <span>线宽</span>
        <div className="drawing-swatches">
          {WIDTHS.map((width) => (
            <button
              key={width}
              type="button"
              className={`drawing-width${selected.style.line_width === width ? " is-active" : ""}`}
              data-testid={`drawing-width-${width}`}
              onClick={() =>
                updateSelected({ style: { ...selected.style, line_width: width } })
              }
            >
              {width}
            </button>
          ))}
        </div>
      </div>

      <label className="drawing-field">
        <span>可见范围</span>
        <select
          data-testid="drawing-visibility"
          value={allTimeframes ? "all" : onlyCurrent ? "current" : "custom"}
          onChange={(event) => {
            const value = event.target.value;
            updateSelected({
              visible_timeframes:
                value === "all" ? [] : value === "current" ? [timeframe] : currentTimeframes,
            });
          }}
        >
          <option value="current">仅当前周期（{timeframe}）</option>
          <option value="all">所有周期</option>
          {!allTimeframes && !onlyCurrent && (
            <option value="custom">自定义（{currentTimeframes.join(" / ")}）</option>
          )}
        </select>
      </label>

      <div className="drawing-inspector-actions">
        <button
          type="button"
          className="ghost-button"
          data-testid="drawing-versions-toggle"
          onClick={toggleVersions}
        >
          版本历史
        </button>
        <button
          type="button"
          className="ghost-button is-danger"
          data-testid="drawing-delete"
          onClick={() => deleteDrawing(selected.drawing_id)}
        >
          删除
        </button>
      </div>

      {showVersions && (
        <div className="drawing-versions" data-testid="drawing-versions">
          {versionsError != null && <p className="drawing-versions-error">{versionsError}</p>}
          {versions == null && versionsError == null && <p>版本读取中…</p>}
          {versions != null && (
            <ul>
              {versions.map((version) => (
                <li key={version.version} className={version.version === selected.version ? "is-current" : ""}>
                  <span>v{version.version}</span>
                  <time>{new Date(version.updated_ts).toLocaleString("zh-CN")}</time>
                  {version.deleted_at != null && <em>已删除</em>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </aside>
  );
}
