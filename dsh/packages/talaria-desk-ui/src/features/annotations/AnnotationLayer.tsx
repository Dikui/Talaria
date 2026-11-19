import { useEffect, useReducer, useState } from "react";
import type { AgentAnnotationRecord } from "@talaria/contracts";
import type { Chart } from "klinecharts";

import { frameSignature, projectPoints, readChartFrame } from "../drawings/projection";
import {
  ANNOTATION_BADGE,
  activeAnnotations,
  annotationPopoverLines,
  annotationPriceLabel,
  annotationZoneEnd,
  evidenceLines,
  kindLabel,
  timeframeMs,
} from "./model";

export interface AnnotationLayerProps {
  chart: Chart | null;
  annotations: readonly AgentAnnotationRecord[];
  timeframe: string;
  readOnly: boolean;
  onAdopt: (annotation: AgentAnnotationRecord) => void;
  onDismiss: (annotation: AgentAnnotationRecord) => void;
}

interface ScreenPoint {
  x: number;
  y: number;
}

function toScreen(
  chart: Chart,
  ts: number,
  price: string,
): ScreenPoint | null {
  const projects = projectPoints(chart, [{ ts, price }]);
  return projects[0] ?? null;
}

/**
 * AGENT_ANNOTATION 图层：只读建议，不参与拖拽/编辑。
 * 虚线 + 半透明带 + 角标，点击只打开溯源浮层；采纳与忽略必须走显式按钮。
 */
export function AnnotationLayer({
  chart,
  annotations,
  timeframe,
  readOnly,
  onAdopt,
  onDismiss,
}: AnnotationLayerProps) {
  const [, forceRender] = useReducer((value: number) => value + 1, 0);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  useEffect(() => {
    if (chart == null) return;
    let raf = 0;
    let last = "";
    const tick = () => {
      const next = frameSignature(chart);
      if (next !== last) {
        last = next;
        forceRender();
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [chart]);

  useEffect(() => {
    if (selectedId !== null && !activeAnnotations(annotations).some(
      (annotation) => annotation.annotation_id === selectedId,
    )) {
      setSelectedId(null);
    }
  }, [annotations, selectedId]);

  if (chart == null) return null;
  const frame = readChartFrame(chart);
  if (frame == null) return null;

  const visible = activeAnnotations(annotations);
  if (visible.length === 0) return null;
  const selected =
    visible.find((annotation) => annotation.annotation_id === selectedId) ?? null;
  const barMs = timeframeMs(timeframe);

  return (
    <>
      <div className="annotation-overlay" data-testid="annotation-overlay">
      <svg
        className="annotation-surface"
        width={frame.width}
        height={frame.height}
        viewBox={`0 0 ${frame.width} ${frame.height}`}
      >
        {visible.map((annotation) => {
          const low = toScreen(chart, annotation.ts_anchor, annotation.price_low);
          if (low == null) return null;
          const isLevel = annotation.kind === "LEVEL";
          const high = isLevel
            ? low
            : toScreen(chart, annotation.ts_anchor, annotation.price_high);
          const end = toScreen(
            chart,
            annotationZoneEnd(annotation, barMs),
            annotation.price_high,
          );
          const x1 = Math.max(0, Math.min(frame.width, low.x));
          const x2 =
            end == null
              ? frame.width
              : Math.max(0, Math.min(frame.width, end.x));
          const y1 = Math.min(low.y, high?.y ?? low.y);
          const y2 = Math.max(low.y, high?.y ?? low.y);
          const active = annotation.annotation_id === selectedId;
          return (
            <g
              key={annotation.annotation_id}
              className={`annotation-shape${active ? " is-selected" : ""}`}
              data-testid={`annotation-shape-${annotation.annotation_id}`}
              onClick={(event) => {
                event.stopPropagation();
                setSelectedId(active ? null : annotation.annotation_id);
              }}
            >
              {isLevel ? (
                <>
                  <line
                    x1={0}
                    x2={frame.width}
                    y1={low.y}
                    y2={low.y}
                    className="annotation-hit"
                  />
                  <line
                    x1={0}
                    x2={frame.width}
                    y1={low.y}
                    y2={low.y}
                    className="annotation-line"
                  />
                </>
              ) : (
                <rect
                  x={x1}
                  y={y1}
                  width={Math.max(1, x2 - x1)}
                  height={Math.max(1, y2 - y1)}
                  className="annotation-zone"
                />
              )}
              <circle cx={Math.max(6, x2)} cy={low.y} r={3} className="annotation-dot" />
              <g
                className="annotation-badge"
                transform={`translate(${Math.max(8, Math.min(frame.width - 150, x1 + 6))}, ${Math.max(14, low.y - 8)})`}
              >
                <rect x={0} y={-13} width={Math.min(148, 52 + annotation.label.length * 12)} height={18} rx={4} />
                <text x={6} y={0}>
                  {ANNOTATION_BADGE} · {kindLabel(annotation.kind)}
                </text>
                <title>{`${annotation.label} ${annotationPriceLabel(annotation)}`}</title>
              </g>
            </g>
          );
        })}
      </svg>
      </div>

      {selected !== null && (
        <div
          className="annotation-popover"
          data-testid="annotation-popover"
          role="dialog"
          aria-label="智能体标注溯源"
        >
          <div className="annotation-popover-head">
            <strong>{selected.label}</strong>
            <button
              type="button"
              className="annotation-popover-close"
              aria-label="关闭标注溯源"
              onClick={() => setSelectedId(null)}
            >
              ×
            </button>
          </div>
          <p className="annotation-popover-price">
            {kindLabel(selected.kind)} · {annotationPriceLabel(selected)}
          </p>
          <ul className="annotation-popover-lines">
            {annotationPopoverLines(selected).map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          {selected.rationale.length > 0 && (
            <p className="annotation-popover-rationale">{selected.rationale}</p>
          )}
          <div className="annotation-evidence">
            <span>证据 {evidenceLines(selected).length} 条</span>
            <ol>
              {evidenceLines(selected).map((line, index) => (
                <li key={`${index}-${line}`}>{line}</li>
              ))}
            </ol>
          </div>
          {!readOnly && (
            <div className="annotation-popover-actions">
              <button
                type="button"
                className="primary-button"
                onClick={() => {
                  onAdopt(selected);
                  setSelectedId(null);
                }}
              >
                采纳为画线
              </button>
              <button
                type="button"
                className="ghost-button"
                onClick={() => {
                  onDismiss(selected);
                  setSelectedId(null);
                }}
              >
                忽略
              </button>
            </div>
          )}
          <p className="annotation-popover-hint">
            快照 {selected.snapshot_id.slice(0, 8)} · 第 {selected.round} 轮 · 只读建议，禁止拖拽
          </p>
        </div>
      )}
    </>
  );
}
