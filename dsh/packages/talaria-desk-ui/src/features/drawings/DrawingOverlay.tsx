import { useEffect, useReducer, useRef, type PointerEvent as ReactPointerEvent } from "react";
import type { Chart } from "klinecharts";
import type { DrawingRecord } from "@talaria/contracts";

import {
  buildDrawingGeometry,
  type DrawingGeometry,
  type GeometryLabel,
} from "./geometry";
import { frameSignature, projectPoints, readChartFrame } from "./projection";
import type { DrawingsController } from "./useDrawings";

export interface DrawingOverlayProps {
  chart: Chart | null;
  controller: DrawingsController;
}

function labelRect(label: GeometryLabel, selected: boolean) {
  const left =
    label.anchor === "end"
      ? label.x - label.width
      : label.anchor === "middle"
        ? label.x - label.width / 2
        : label.x;
  return { left, top: label.y, width: label.width, height: label.height, selected };
}

function strokeWidth(selected: boolean): number {
  return selected ? 2 : 1.4;
}

export function DrawingOverlay({ chart, controller }: DrawingOverlayProps) {
  const { drawings, draft, selected, readOnly, beginDrag, dragTo, endDrag, select } =
    controller;
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [, forceRender] = useReducer((value: number) => value + 1, 0);
  const draggingRef = useRef<string | null>(null);

  // KLineChart 的平移/缩放/实时价格重算不一定触发事件，用逐帧签名比较兜底对齐。
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

  if (chart == null) return null;
  const frame = readChartFrame(chart);
  if (frame == null) return null;
  const size = { width: frame.width, height: frame.height };

  const localPoint = (event: ReactPointerEvent): { x: number; y: number } | null => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (rect == null) return null;
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  };

  const projectRecord = (record: DrawingRecord): DrawingGeometry | null => {
    const pixels = projectPoints(chart, record.points);
    if (pixels.some((pixel) => pixel == null)) return null;
    const anchors = pixels.map((pixel, index) => ({
      x: pixel!.x,
      y: pixel!.y,
      price: record.points[index].price,
    }));
    return buildDrawingGeometry(record.kind, anchors, {
      size,
      text: record.text,
    });
  };

  const draftPixels = projectPoints(chart, draft);
  const selectedId = selected?.drawing_id ?? null;

  return (
    <div className="drawing-overlay" data-testid="drawing-overlay">
      <svg
        ref={svgRef}
        className="drawing-surface"
        width={frame.width}
        height={frame.height}
        viewBox={`0 0 ${frame.width} ${frame.height}`}
      >
        {drawings.map((record) => {
          const geometry = projectRecord(record);
          if (geometry == null) return null;
          const isSelected = selectedId === record.drawing_id;
          const sw = strokeWidth(isSelected);
          const onPointerDown = (event: ReactPointerEvent<SVGGElement>) => {
            event.stopPropagation();
            event.preventDefault();
            const point = localPoint(event);
            beginDrag(record.drawing_id, point?.x ?? 0, point?.y ?? 0);
            if (point == null) return;
            draggingRef.current = record.drawing_id;
            event.currentTarget.setPointerCapture(event.pointerId);
          };
          const onPointerMove = (event: ReactPointerEvent<SVGGElement>) => {
            if (draggingRef.current !== record.drawing_id) return;
            const point = localPoint(event);
            if (point != null) dragTo(point.x, point.y);
          };
          const finish = (event: ReactPointerEvent<SVGGElement>) => {
            if (draggingRef.current !== record.drawing_id) return;
            draggingRef.current = null;
            endDrag();
            event.currentTarget.releasePointerCapture?.(event.pointerId);
          };
          return (
            <g
              key={record.drawing_id}
              className={`drawing-shape${isSelected ? " is-selected" : ""}`}
              data-testid={`drawing-shape-${record.drawing_id}`}
              data-kind={record.kind}
              style={{ pointerEvents: "auto", cursor: readOnly ? "pointer" : "move" }}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={finish}
              onPointerCancel={finish}
              onClick={(event) => {
                event.stopPropagation();
                if (readOnly) select(record.drawing_id);
              }}
            >
              {geometry.lines.map((line, index) => (
                <g key={`line-${index}`}>
                  <line
                    x1={line.x1}
                    y1={line.y1}
                    x2={line.x2}
                    y2={line.y2}
                    stroke="transparent"
                    strokeWidth={12}
                  />
                  <line
                    x1={line.x1}
                    y1={line.y1}
                    x2={line.x2}
                    y2={line.y2}
                    stroke={record.style.color}
                    strokeWidth={sw}
                    strokeDasharray={line.dashed ? "5 4" : undefined}
                  />
                </g>
              ))}
              {geometry.rects.map((rect, index) => (
                <g key={`rect-${index}`}>
                  <rect
                    x={rect.x}
                    y={rect.y}
                    width={rect.width}
                    height={rect.height}
                    fill={record.style.color}
                    fillOpacity={record.style.fill_opacity ?? 0.12}
                    stroke={record.style.color}
                    strokeWidth={sw}
                  />
                </g>
              ))}
              {geometry.polygons.map((polygon, index) => (
                <polygon
                  key={`polygon-${index}`}
                  points={polygon.points.map((point) => `${point.x},${point.y}`).join(" ")}
                  fill={record.style.color}
                  fillOpacity={(record.style.fill_opacity ?? 0.12) / 2}
                  stroke={record.style.color}
                  strokeWidth={sw}
                  strokeDasharray="5 4"
                />
              ))}
              {geometry.labels.map((label, index) => {
                const box = labelRect(label, isSelected);
                return (
                  <g key={`label-${index}`} className={`drawing-label drawing-label--${label.role}`}>
                    <rect
                      x={box.left - 2}
                      y={box.top}
                      width={box.width + 4}
                      height={box.height}
                      rx={3}
                      className="drawing-label-bg"
                    />
                    <text x={label.x} y={label.y + 13} textAnchor={label.anchor}>
                      {label.text}
                    </text>
                  </g>
                );
              })}
              {isSelected &&
                geometry.anchors.map((anchor, index) => (
                  <circle
                    key={`anchor-${index}`}
                    cx={anchor.x}
                    cy={anchor.y}
                    r={3.5}
                    className="drawing-anchor-dot"
                  />
                ))}
              <title>
                {record.kind} v{record.version}
              </title>
            </g>
          );
        })}
        {draftPixels.map((pixel, index) =>
          pixel == null ? null : (
            <circle
              key={`draft-${index}`}
              cx={pixel.x}
              cy={pixel.y}
              r={4}
              className="drawing-draft-dot"
            />
          ),
        )}
      </svg>
    </div>
  );
}
