import type { DrawingPatch, DrawingRecord } from "@talaria/contracts";

/**
 * 前端撤销/重做基于后端版本链：每个操作都对应一次可逆的 REST 调用。
 * - update: PATCH before/after
 * - create: 撤销 DELETE、重做 RESTORE
 * - delete: 撤销 RESTORE、重做 DELETE
 * - clear : 撤销逐条 RESTORE、重做逐条 DELETE
 */
export type DrawingHistoryOperation =
  | {
      kind: "update";
      drawingId: string;
      before: DrawingPatch;
      after: DrawingPatch;
    }
  | { kind: "create"; drawingId: string }
  | { kind: "delete"; drawingId: string }
  | { kind: "clear"; drawingIds: string[] };

export interface DrawingHistory {
  undo: DrawingHistoryOperation[];
  redo: DrawingHistoryOperation[];
}

const HISTORY_LIMIT = 100;

export function emptyHistory(): DrawingHistory {
  return { undo: [], redo: [] };
}

export function recordOperation(
  history: DrawingHistory,
  operation: DrawingHistoryOperation,
): DrawingHistory {
  const undo = [...history.undo, operation];
  return {
    undo: undo.length > HISTORY_LIMIT ? undo.slice(-HISTORY_LIMIT) : undo,
    redo: [],
  };
}

export interface HistoryStep {
  operation: DrawingHistoryOperation;
  history: DrawingHistory;
}

export function takeUndo(history: DrawingHistory): HistoryStep | null {
  const operation = history.undo[history.undo.length - 1];
  if (operation === undefined) {
    return null;
  }
  return {
    operation,
    history: {
      undo: history.undo.slice(0, -1),
      redo: [...history.redo, operation],
    },
  };
}

export function takeRedo(history: DrawingHistory): HistoryStep | null {
  const operation = history.redo[history.redo.length - 1];
  if (operation === undefined) {
    return null;
  }
  return {
    operation,
    history: {
      undo: [...history.undo, operation],
      redo: history.redo.slice(0, -1),
    },
  };
}

/** 乐观重绘：把 PATCH 立即应用到本地记录，等后端版本链确认后再替换。 */
export function applyDrawingPatch(
  record: DrawingRecord,
  patch: DrawingPatch,
): DrawingRecord {
  return {
    ...record,
    points: patch.points != null ? patch.points : record.points,
    text: patch.text != null ? patch.text : record.text,
    style:
      patch.style != null ? { ...record.style, ...patch.style } : record.style,
    visible_timeframes:
      patch.visible_timeframes != null
        ? patch.visible_timeframes
        : record.visible_timeframes,
  };
}
