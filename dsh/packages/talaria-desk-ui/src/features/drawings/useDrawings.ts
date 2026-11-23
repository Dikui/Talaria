import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Chart } from "klinecharts";
import type {
  DrawingKind,
  DrawingPatch,
  DrawingPoint,
  DrawingRecord,
} from "@talaria/contracts";

import { api } from "../../lib/api";
import { t, useLocale } from "../../lib/i18n";
import type { PanelLink } from "../../workbench/link";
import {
  applyDrawingPatch,
  emptyHistory,
  recordOperation,
  takeRedo,
  takeUndo,
  type DrawingHistory,
  type DrawingHistoryOperation,
} from "./history";
import { projectPoints, readChartFrame, unprojectPoint, visiblePriceWindow } from "./projection";
import type { PixelPoint } from "./geometry";
import { timeWindow } from "./viewport";
import { createSerialQueue } from "./writeQueue";
import {
  anchorsRequired,
  defaultDrawingStyle,
  toolDefinition,
  visibleTimeframesFor,
  type DrawingToolId,
} from "./tools";
import type { DrawingToolDefinition } from "./tools";

export interface DrawingsController {
  drawings: DrawingRecord[];
  allDrawings: DrawingRecord[];
  selected: DrawingRecord | null;
  activeTool: DrawingToolId;
  draft: DrawingPoint[];
  toolHint: string;
  loading: boolean;
  pending: number;
  error: string | null;
  canUndo: boolean;
  canRedo: boolean;
  readOnly: boolean;
  setTool(tool: DrawingToolId): void;
  select(id: string | null): void;
  addPointAt(x: number, y: number): void;
  cancelDraft(): void;
  beginDrag(id: string, x: number, y: number): void;
  dragTo(x: number, y: number): void;
  endDrag(): void;
  updateSelected(patch: DrawingPatch): void;
  deleteDrawing(id: string): void;
  clearInstrument(): void;
  undo(): void;
  redo(): void;
  refresh(): void;
  clearError(): void;
}

export interface UseDrawingsOptions {
  chart: Chart | null;
  instrumentId: string;
  timeframe: string;
  pricePrecision: number;
  readOnly: boolean;
  link: Pick<PanelLink, "subscribeDrawings" | "notifyDrawingsChanged">;
}

function describeError(error: unknown): string {
  if (error instanceof Error && error.message.length > 0) {
    return error.message;
  }
  return t("画线同步失败，请重试", "Drawing sync failed. Please retry.");
}

let optimisticSeq = 0;

function optimisticRecord(
  instrumentId: string,
  kind: DrawingKind,
  points: DrawingPoint[],
  text: string,
  visibleTimeframes: string[],
): DrawingRecord {
  optimisticSeq += 1;
  const now = Date.now();
  return {
    drawing_id: `pending_${now}_${optimisticSeq}`,
    instrument_id: instrumentId,
    kind,
    visible_timeframes: visibleTimeframes,
    points,
    text,
    style: defaultDrawingStyle(),
    version: 0,
    created_ts: now,
    updated_ts: now,
    deleted_at: null,
  };
}

function samePoints(a: DrawingPoint[], b: DrawingPoint[]): boolean {
  return (
    a.length === b.length &&
    a.every((point, index) => point.ts === b[index].ts && point.price === b[index].price)
  );
}

/** 画线状态机：乐观重绘 + 单一异步串行写队列 + 前端版本链撤销/重做。 */
export function useDrawings(options: UseDrawingsOptions): DrawingsController {
  const { chart, instrumentId, timeframe, pricePrecision, readOnly, link } = options;
  const locale = useLocale();
  const [drawings, setDrawings] = useState<DrawingRecord[]>([]);
  const [allDrawings, setAllDrawings] = useState<DrawingRecord[]>([]);
  const [catalogRevision, setCatalogRevision] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [activeTool, setActiveTool] = useState<DrawingToolId>("select");
  const [draft, setDraft] = useState<DrawingPoint[]>([]);
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [historyVersion, setHistoryVersion] = useState(0);

  const queueRef = useRef(createSerialQueue());
  const historyRef = useRef<DrawingHistory>(emptyHistory());
  const drawingsRef = useRef<DrawingRecord[]>([]);
  const chartRef = useRef<Chart | null>(chart);
  const requestRef = useRef(0);
  const dragRef = useRef<{
    id: string;
    startPoints: DrawingPoint[];
    startPixels: Array<PixelPoint | null>;
    startX: number;
    startY: number;
    moved: boolean;
  } | null>(null);

  chartRef.current = chart;
  drawingsRef.current = drawings;

  const bumpHistory = useCallback(() => {
    setHistoryVersion((value) => value + 1);
  }, []);

  const pushHistory = useCallback(
    (operation: DrawingHistoryOperation) => {
      historyRef.current = recordOperation(historyRef.current, operation);
      bumpHistory();
    },
    [bumpHistory],
  );

  const enqueue = useCallback(<T,>(task: () => Promise<T>): Promise<T> => {
    setPending((value) => value + 1);
    const promise = queueRef.current.run(async () => {
      const result = await task();
      link.notifyDrawingsChanged(instrumentId);
      return result;
    });
    promise
      .catch(() => undefined)
      .finally(() => setPending((value) => Math.max(0, value - 1)));
    return promise;
  }, [instrumentId, link]);

  const loadWindow = useCallback(
    async (target: Chart): Promise<void> => {
      const frame = readChartFrame(target);
      const range = target.getVisibleRange();
      const window = timeWindow(range, target.getDataList());
      const prices = visiblePriceWindow(target, pricePrecision);
      if (frame == null || window == null || prices == null) {
        return;
      }
      const requestId = requestRef.current + 1;
      requestRef.current = requestId;
      setLoading(true);
      try {
        const view = await api.chartView({
          instrument_id: instrumentId,
          timeframe,
          from_ts: window.from_ts,
          to_ts: window.to_ts,
          price_min: prices.price_min,
          price_max: prices.price_max,
        });
        if (requestRef.current !== requestId) return;
        setDrawings(view.drawings);
        setError(null);
      } catch (caught) {
        if (requestRef.current === requestId) {
          setError(describeError(caught));
        }
      } finally {
        if (requestRef.current === requestId) {
          setLoading(false);
        }
      }
    },
    [instrumentId, timeframe, pricePrecision],
  );

  useEffect(() => {
    let active = true;
    if (pending === 0) {
      api.drawings(instrumentId, timeframe, { all_timeframes: true }).then(({ drawings }) => {
        if (active) setAllDrawings(drawings);
      }).catch(error => {
        if (active) {
          setAllDrawings([]);
          setError(describeError(error));
        }
      });
    }
    return () => { active = false; };
  }, [instrumentId, timeframe, pending, catalogRevision]);

  const refresh = useCallback(() => {
    setCatalogRevision(value => value + 1);
    const target = chartRef.current;
    if (target == null) return;
    void loadWindow(target);
  }, [loadWindow]);

  useEffect(() => {
    let disposed = false;
    const stop = link.subscribeDrawings(instrumentId, () => {
      void queueRef.current.idle().then(() => { if (!disposed) refresh(); });
    });
    return () => {
      disposed = true;
      stop();
    };
  }, [instrumentId, link, refresh]);

  // 切换标的/周期/图表实例：清空本地状态并按新可见范围恢复。
  useEffect(() => {
    setDrawings([]);
    setSelectedId(null);
    setDraft([]);
    setActiveTool("select");
    setError(null);
    historyRef.current = emptyHistory();
    bumpHistory();
    requestRef.current += 1;
    if (chart == null) return;
    let disposed = false;
    let raf = 0;
    let frames = 0;
    const waitForData = () => {
      if (disposed) return;
      frames += 1;
      if (chart.getDataList().length > 0 || frames > 240) {
        void loadWindow(chart);
        return;
      }
      raf = requestAnimationFrame(waitForData);
    };
    raf = requestAnimationFrame(waitForData);
    return () => {
      disposed = true;
      cancelAnimationFrame(raf);
      requestRef.current += 1;
    };
  }, [chart, instrumentId, timeframe, loadWindow, bumpHistory]);

  // 平移/缩放后按可见范围重新拉取；去抖避免拖动期间刷屏。
  useEffect(() => {
    if (chart == null) return;
    let timer: number | undefined;
    const schedule = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => void loadWindow(chart), 180);
    };
    chart.subscribeAction("onVisibleRangeChange", schedule);
    chart.subscribeAction("onScroll", schedule);
    chart.subscribeAction("onZoom", schedule);
    return () => {
      window.clearTimeout(timer);
      chart.unsubscribeAction("onVisibleRangeChange", schedule);
      chart.unsubscribeAction("onScroll", schedule);
      chart.unsubscribeAction("onZoom", schedule);
    };
  }, [chart, loadWindow]);

  const select = useCallback((id: string | null) => {
    setSelectedId(id);
  }, []);

  const setTool = useCallback(
    (tool: DrawingToolId) => {
      if (readOnly) return;
      setActiveTool(tool);
      setDraft([]);
    },
    [readOnly],
  );

  const cancelDraft = useCallback(() => {
    setDraft([]);
  }, []);

  const selected = useMemo(
    () => drawings.find((drawing) => drawing.drawing_id === selectedId) ?? null,
    [drawings, selectedId],
  );

  const createFromPoints = useCallback(
    (definition: DrawingToolDefinition, points: DrawingPoint[]) => {
      const kind = definition.kind;
      if (kind == null) return;
      const text = kind === "TEXT_LABEL" ? t("标注", "Annotation") : "";
      const optimistic = optimisticRecord(
        instrumentId,
        kind,
        points,
        text,
        visibleTimeframesFor(timeframe),
      );
      setDrawings((current) => [...current, optimistic]);
      setSelectedId(optimistic.drawing_id);
      enqueue(async () => {
        const saved = await api.createDrawing({
          instrument_id: instrumentId,
          kind,
          visible_timeframes: optimistic.visible_timeframes,
          points,
          text,
          style: optimistic.style,
        });
        setDrawings((current) =>
          current.map((drawing) =>
            drawing.drawing_id === optimistic.drawing_id ? saved : drawing,
          ),
        );
        setSelectedId((current) =>
          current === optimistic.drawing_id ? saved.drawing_id : current,
        );
        pushHistory({ kind: "create", drawingId: saved.drawing_id });
      }).catch((caught) => {
        setDrawings((current) =>
          current.filter((drawing) => drawing.drawing_id !== optimistic.drawing_id),
        );
        setError(describeError(caught));
      });
    },
    [enqueue, instrumentId, pushHistory, timeframe],
  );

  const addPointAt = useCallback(
    (x: number, y: number) => {
      const target = chartRef.current;
      if (readOnly || target == null) return;
      const definition = toolDefinition(activeTool);
      if (definition == null || definition.kind == null) return;
      const plot = unprojectPoint(target, x, y, pricePrecision);
      if (plot == null) return;
      const required = anchorsRequired(definition.kind);
      const next = [...draft, { ts: plot.ts, price: plot.price }];
      if (next.length < required) {
        setDraft(next);
        return;
      }
      setDraft([]);
      createFromPoints(definition, next.slice(0, required));
    },
    [activeTool, createFromPoints, draft, pricePrecision, readOnly],
  );

  // 工具激活时在图表 DOM 上捕获"点击空白处"（覆盖层 pointer-events:none，不挡平移缩放）。
  useEffect(() => {
    if (chart == null || readOnly || activeTool === "select") return;
    const dom = chart.getDom();
    if (dom == null) return;
    let down: { x: number; y: number } | null = null;
    const onPointerDown = (event: PointerEvent) => {
      down = { x: event.clientX, y: event.clientY };
    };
    const onClick = (event: MouseEvent) => {
      const start = down;
      down = null;
      if (start == null) return;
      if (Math.hypot(event.clientX - start.x, event.clientY - start.y) > 4) return;
      const rect = dom.getBoundingClientRect();
      addPointAt(event.clientX - rect.left, event.clientY - rect.top);
    };
    dom.addEventListener("pointerdown", onPointerDown);
    dom.addEventListener("click", onClick);
    return () => {
      dom.removeEventListener("pointerdown", onPointerDown);
      dom.removeEventListener("click", onClick);
    };
  }, [chart, readOnly, activeTool, addPointAt]);

  useEffect(() => {
    if (readOnly) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setDraft([]);
        setSelectedId(null);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [readOnly]);

  const beginDrag = useCallback(
    (id: string, x: number, y: number) => {
      const target = chartRef.current;
      setSelectedId(id);
      if (readOnly || target == null) return;
      const record = drawingsRef.current.find((drawing) => drawing.drawing_id === id);
      if (record == null) return;
      dragRef.current = {
        id,
        startPoints: record.points.map((point) => ({ ...point })),
        startPixels: projectPoints(target, record.points),
        startX: x,
        startY: y,
        moved: false,
      };
    },
    [readOnly],
  );

  const dragTo = useCallback(
    (x: number, y: number) => {
      const drag = dragRef.current;
      const target = chartRef.current;
      if (drag == null || target == null) return;
      const dx = x - drag.startX;
      const dy = y - drag.startY;
      if (!drag.moved && Math.hypot(dx, dy) < 2) return;
      drag.moved = true;
      const next = drag.startPoints.map((point, index) => {
        const start = drag.startPixels[index];
        if (start == null) return point;
        const plot = unprojectPoint(target, start.x + dx, start.y + dy, pricePrecision);
        return plot == null ? point : { ts: plot.ts, price: plot.price };
      });
      setDrawings((current) =>
        current.map((drawing) =>
          drawing.drawing_id === drag.id ? { ...drawing, points: next } : drawing,
        ),
      );
    },
    [pricePrecision],
  );

  const endDrag = useCallback(() => {
    const drag = dragRef.current;
    dragRef.current = null;
    if (drag == null || !drag.moved) return;
    const record = drawingsRef.current.find(
      (drawing) => drawing.drawing_id === drag.id,
    );
    if (record == null || samePoints(record.points, drag.startPoints)) return;
    const points = record.points;
    enqueue(() => api.patchDrawing(drag.id, { points }))
      .then((saved) => {
        setDrawings((current) =>
          current.map((drawing) =>
            drawing.drawing_id === drag.id ? saved : drawing,
          ),
        );
        pushHistory({
          kind: "update",
          drawingId: drag.id,
          before: { points: drag.startPoints },
          after: { points },
        });
      })
      .catch((caught) => {
        setDrawings((current) =>
          current.map((drawing) =>
            drawing.drawing_id === drag.id
              ? { ...drawing, points: drag.startPoints }
              : drawing,
          ),
        );
        setError(describeError(caught));
      });
  }, [enqueue, pushHistory]);

  const updateSelected = useCallback(
    (patch: DrawingPatch) => {
      if (readOnly) return;
      const record = drawingsRef.current.find(
        (drawing) => drawing.drawing_id === selectedId,
      );
      if (record == null) return;
      const before: DrawingPatch = {};
      if (patch.points !== undefined) before.points = record.points;
      if (patch.text !== undefined) before.text = record.text;
      if (patch.style !== undefined) before.style = record.style;
      if (patch.visible_timeframes !== undefined) {
        before.visible_timeframes = record.visible_timeframes;
      }
      const id = record.drawing_id;
      setDrawings((current) =>
        current.map((drawing) =>
          drawing.drawing_id === id ? applyDrawingPatch(drawing, patch) : drawing,
        ),
      );
      enqueue(() => api.patchDrawing(id, patch))
        .then((saved) => {
          setDrawings((current) =>
            current.map((drawing) => (drawing.drawing_id === id ? saved : drawing)),
          );
          pushHistory({ kind: "update", drawingId: id, before, after: patch });
        })
        .catch((caught) => {
          setDrawings((current) =>
            current.map((drawing) => (drawing.drawing_id === id ? record : drawing)),
          );
          setError(describeError(caught));
        });
    },
    [enqueue, pushHistory, readOnly, selectedId],
  );

  const deleteDrawing = useCallback(
    (id: string) => {
      if (readOnly) return;
      const record = drawingsRef.current.find((drawing) => drawing.drawing_id === id);
      if (record == null) return;
      setDrawings((current) => current.filter((drawing) => drawing.drawing_id !== id));
      setSelectedId((current) => (current === id ? null : current));
      enqueue(() => api.deleteDrawing(id))
        .then(() => {
          pushHistory({ kind: "delete", drawingId: id });
        })
        .catch((caught) => {
          setDrawings((current) => [...current, record]);
          setError(describeError(caught));
        });
    },
    [enqueue, pushHistory, readOnly],
  );

  const clearInstrument = useCallback(() => {
    if (readOnly) return;
    const ids = drawingsRef.current.map((drawing) => drawing.drawing_id);
    const snapshot = drawingsRef.current;
    setDrawings([]);
    setSelectedId(null);
    setDraft([]);
    enqueue(() => api.clearDrawings(instrumentId))
      .then((response) => {
        if (ids.length > 0 && response.cleared > 0) {
          pushHistory({ kind: "clear", drawingIds: ids });
        }
      })
      .catch((caught) => {
        setDrawings(snapshot);
        setError(describeError(caught));
      });
  }, [enqueue, instrumentId, pushHistory, readOnly]);

  const applyOperation = useCallback(
    async (operation: DrawingHistoryOperation, direction: "undo" | "redo") => {
      if (readOnly) return;
      switch (operation.kind) {
        case "update": {
          const patch = direction === "undo" ? operation.before : operation.after;
          const record = drawingsRef.current.find(
            (drawing) => drawing.drawing_id === operation.drawingId,
          );
          if (record == null) return;
          setDrawings((current) =>
            current.map((drawing) =>
              drawing.drawing_id === operation.drawingId
                ? applyDrawingPatch(drawing, patch)
                : drawing,
            ),
          );
          const saved = await enqueue(() =>
            api.patchDrawing(operation.drawingId, patch),
          );
          setDrawings((current) =>
            current.map((drawing) =>
              drawing.drawing_id === operation.drawingId ? saved : drawing,
            ),
          );
          return;
        }
        case "create": {
          if (direction === "undo") {
            setDrawings((current) =>
              current.filter(
                (drawing) => drawing.drawing_id !== operation.drawingId,
              ),
            );
            await enqueue(() => api.deleteDrawing(operation.drawingId));
            return;
          }
          const saved = await enqueue(() => api.restoreDrawing(operation.drawingId));
          setDrawings((current) =>
            current.some((drawing) => drawing.drawing_id === saved.drawing_id)
              ? current
              : [...current, saved],
          );
          return;
        }
        case "delete": {
          if (direction === "undo") {
            const saved = await enqueue(() =>
              api.restoreDrawing(operation.drawingId),
            );
            setDrawings((current) =>
              current.some((drawing) => drawing.drawing_id === saved.drawing_id)
                ? current
                : [...current, saved],
            );
            return;
          }
          setDrawings((current) =>
            current.filter((drawing) => drawing.drawing_id !== operation.drawingId),
          );
          await enqueue(() => api.deleteDrawing(operation.drawingId));
          return;
        }
        case "clear": {
          if (direction === "undo") {
            for (const id of operation.drawingIds) {
              const saved = await enqueue(() => api.restoreDrawing(id));
              setDrawings((current) =>
                current.some((drawing) => drawing.drawing_id === saved.drawing_id)
                  ? current
                  : [...current, saved],
              );
            }
            return;
          }
          setDrawings((current) =>
            current.filter(
              (drawing) => !operation.drawingIds.includes(drawing.drawing_id),
            ),
          );
          for (const id of operation.drawingIds) {
            await enqueue(() => api.deleteDrawing(id));
          }
          return;
        }
        default:
          return;
      }
    },
    [enqueue, readOnly],
  );

  const undo = useCallback(() => {
    const step = takeUndo(historyRef.current);
    if (step == null) return;
    historyRef.current = step.history;
    bumpHistory();
    void applyOperation(step.operation, "undo").catch((caught) => {
      setError(describeError(caught));
      historyRef.current = {
        undo: [...historyRef.current.undo, step.operation],
        redo: historyRef.current.redo,
      };
      bumpHistory();
    });
  }, [applyOperation, bumpHistory]);

  const redo = useCallback(() => {
    const step = takeRedo(historyRef.current);
    if (step == null) return;
    historyRef.current = step.history;
    bumpHistory();
    void applyOperation(step.operation, "redo").catch((caught) => {
      setError(describeError(caught));
      historyRef.current = {
        undo: historyRef.current.undo,
        redo: [...historyRef.current.redo, step.operation],
      };
      bumpHistory();
    });
  }, [applyOperation, bumpHistory]);

  const toolHint = useMemo(() => {
    const definition = toolDefinition(activeTool);
    if (definition == null) return t("选择画线可拖拽移动，Esc 取消选中", "Select a drawing to drag it. Press Esc to deselect.");
    const label = locale === "zh" ? definition.label : definition.labelEn;
    const hint = locale === "zh" ? definition.hint : definition.hintEn;
    return t(
      `${label}：${hint}，已完成 ${draft.length}/${definition.anchors}`,
      `${label}: ${hint}. ${draft.length}/${definition.anchors} points placed.`,
    );
  }, [activeTool, draft.length, locale]);

  return {
    drawings, allDrawings,
    selected,
    activeTool,
    draft,
    toolHint,
    loading,
    pending,
    error,
    canUndo: historyVersion >= 0 && historyRef.current.undo.length > 0,
    canRedo: historyVersion >= 0 && historyRef.current.redo.length > 0,
    readOnly,
    setTool,
    select,
    addPointAt,
    cancelDraft,
    beginDrag,
    dragTo,
    endDrag,
    updateSelected,
    deleteDrawing,
    clearInstrument,
    undo,
    redo,
    refresh,
    clearError: () => setError(null),
  };
}
