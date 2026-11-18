import { describe, expect, it } from "vitest";

import type { DrawingRecord } from "@talaria/contracts";

import {
  applyDrawingPatch,
  emptyHistory,
  recordOperation,
  takeRedo,
  takeUndo,
  type DrawingHistory,
} from "./history";

const UPDATE = {
  kind: "update" as const,
  drawingId: "drw_1",
  before: { text: "旧" },
  after: { text: "新" },
};

describe("undo/redo 版本链", () => {
  it("记录新操作会清空重做栈", () => {
    let history: DrawingHistory = emptyHistory();
    history = takeUndo(recordOperation(history, UPDATE))!.history;
    expect(history.redo).toHaveLength(1);
    history = recordOperation(history, { kind: "delete", drawingId: "drw_2" });
    expect(history.undo).toHaveLength(1);
    expect(history.redo).toHaveLength(0);
  });

  it("撤销后把操作放进重做栈，重做后放回撤销栈", () => {
    let history = recordOperation(emptyHistory(), UPDATE);
    const undoResult = takeUndo(history);
    expect(undoResult?.operation).toEqual(UPDATE);
    history = undoResult!.history;
    expect(history.undo).toHaveLength(0);
    expect(history.redo).toEqual([UPDATE]);

    const redoResult = takeRedo(history);
    expect(redoResult?.operation).toEqual(UPDATE);
    history = redoResult!.history;
    expect(history.undo).toEqual([UPDATE]);
    expect(history.redo).toHaveLength(0);
  });

  it("空栈撤销/重做返回 null，不抛错", () => {
    expect(takeUndo(emptyHistory())).toBeNull();
    expect(takeRedo(emptyHistory())).toBeNull();
  });

  it("撤销栈有上限，避免长时间会话无限增长", () => {
    let history: DrawingHistory = emptyHistory();
    for (let index = 0; index < 120; index += 1) {
      history = recordOperation(history, {
        kind: "delete",
        drawingId: `drw_${index}`,
      });
    }
    expect(history.undo).toHaveLength(100);
    expect(history.undo[0]).toEqual({ kind: "delete", drawingId: "drw_20" });
  });
});

describe("applyDrawingPatch", () => {
  const RECORD: DrawingRecord = {
    drawing_id: "drw_1",
    instrument_id: "BTC-USDT-PERP",
    kind: "SEGMENT",
    visible_timeframes: ["1h"],
    points: [
      { ts: 1, price: "100.00" },
      { ts: 2, price: "101.00" },
    ],
    text: "旧文本",
    style: { color: "#4c8dff", line_width: 1, line_style: "solid", fill_opacity: 0.12 },
    version: 1,
    created_ts: 10,
    updated_ts: 10,
    deleted_at: null,
  };

  it("点位/文本/样式/周期的局部更新都不会丢字段", () => {
    const patched = applyDrawingPatch(RECORD, {
      points: [
        { ts: 3, price: "99.00" },
        { ts: 4, price: "102.00" },
      ],
      text: "新文本",
      style: { color: "#f2545b" },
      visible_timeframes: [],
    });
    expect(patched.points[0].price).toBe("99.00");
    expect(patched.text).toBe("新文本");
    expect(patched.style.color).toBe("#f2545b");
    expect(patched.style.line_width).toBe(1);
    expect(patched.visible_timeframes).toEqual([]);
  });

  it("未提供的字段与 null 都保持原值", () => {
    const patched = applyDrawingPatch(RECORD, { text: null, style: null });
    expect(patched.text).toBe("旧文本");
    expect(patched.style).toEqual(RECORD.style);
    expect(patched.points).toEqual(RECORD.points);
  });
});
