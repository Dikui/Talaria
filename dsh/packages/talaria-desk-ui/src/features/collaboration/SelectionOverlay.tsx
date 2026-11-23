import { useCallback, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import type { Chart } from "klinecharts";

import type { CollabWindow } from "@talaria/contracts";

import { t, useLocale } from "../../lib/i18n";
import { unprojectPoint } from "../drawings/projection";

interface SelectionOverlayProps {
  chart: Chart | null;
  active: boolean;
  pricePrecision: number;
  onSelect: (window: CollabWindow) => void;
}

interface DragState {
  startX: number;
  startY: number;
  currentX: number;
  currentY: number;
}

function relativePoint(
  event: ReactPointerEvent<HTMLDivElement>,
  rect: DOMRect,
): { x: number; y: number } {
  return {
    x: Math.max(0, Math.min(rect.width, event.clientX - rect.left)),
    y: Math.max(0, Math.min(rect.height, event.clientY - rect.top)),
  };
}

/** 只读框选层：把像素矩形反投影成时间/价格闭区间，不写任何画线或状态。 */
export function SelectionOverlay({
  chart,
  active,
  pricePrecision,
  onSelect,
}: SelectionOverlayProps) {
  const [drag, setDrag] = useState<DragState | null>(null);
  useLocale();

  const finish = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (drag === null || chart === null) {
        setDrag(null);
        return;
      }
      const rect = event.currentTarget.getBoundingClientRect();
      const point = relativePoint(event, rect);
      const left = Math.min(drag.startX, point.x);
      const right = Math.max(drag.startX, point.x);
      const top = Math.min(drag.startY, point.y);
      const bottom = Math.max(drag.startY, point.y);
      setDrag(null);
      if (right - left < 6 || bottom - top < 6) return;
      const first = unprojectPoint(chart, left, top, pricePrecision);
      const second = unprojectPoint(chart, right, bottom, pricePrecision);
      if (first === null || second === null) return;
      onSelect({
        from_ts: Math.min(first.ts, second.ts),
        to_ts: Math.max(first.ts, second.ts),
        price_min:
          Number(first.price) <= Number(second.price)
            ? first.price
            : second.price,
        price_max:
          Number(first.price) <= Number(second.price)
            ? second.price
            : first.price,
      });
    },
    [chart, drag, onSelect, pricePrecision],
  );

  if (!active) return null;

  const rect =
    drag === null
      ? null
      : {
          left: Math.min(drag.startX, drag.currentX),
          top: Math.min(drag.startY, drag.currentY),
          width: Math.abs(drag.currentX - drag.startX),
          height: Math.abs(drag.currentY - drag.startY),
        };

  return (
    <div
      className={`collaboration-selection-layer${drag === null ? "" : " is-dragging"}`}
      data-testid="collaboration-selection"
      role="application"
      aria-label={t("框选会诊区间：拖拽选择时间与价格范围", "Select a consultation range: drag to choose time and price")}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        const point = relativePoint(
          event,
          event.currentTarget.getBoundingClientRect(),
        );
        event.currentTarget.setPointerCapture(event.pointerId);
        setDrag({
          startX: point.x,
          startY: point.y,
          currentX: point.x,
          currentY: point.y,
        });
      }}
      onPointerMove={(event) => {
        if (drag === null) return;
        const point = relativePoint(
          event,
          event.currentTarget.getBoundingClientRect(),
        );
        setDrag({ ...drag, currentX: point.x, currentY: point.y });
      }}
      onPointerUp={finish}
      onPointerCancel={() => setDrag(null)}
    >
      {rect === null ? (
        <div className="collaboration-selection-hint">
          {t("拖拽框选时间与价格区间 · Esc 取消", "Drag to select time and price range · Esc to cancel")}
        </div>
      ) : (
        <div
          className="collaboration-selection-box"
          style={{
            left: rect.left,
            top: rect.top,
            width: rect.width,
            height: rect.height,
          }}
        />
      )}
    </div>
  );
}
