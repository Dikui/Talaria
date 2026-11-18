import { describe, expect, it } from "vitest";

import { book, INSTRUMENT, kline } from "./testData";
import { parseDeskMessage } from "./messages";

describe("parseDeskMessage", () => {
  it("解析 hello 并保留 live 标记", () => {
    const parsed = parseDeskMessage(
      JSON.stringify({
        type: "hello",
        feed_mode: "replay",
        server_ts: 1700000000000,
        live: true,
      }),
    );
    expect(parsed).toEqual({
      type: "hello",
      feed_mode: "replay",
      server_ts: 1700000000000,
      live: true,
    });
  });

  it("解析 bar / book / health 的 data 载荷", () => {
    const barMessage = parseDeskMessage(
      JSON.stringify({ type: "bar", data: kline(1_763_596_800_000) }),
    );
    expect(barMessage?.type).toBe("bar");

    const bookMessage = parseDeskMessage(
      JSON.stringify({ type: "book", data: book() }),
    );
    expect(bookMessage?.type).toBe("book");

    const healthMessage = parseDeskMessage(
      JSON.stringify({
        type: "health",
        data: {
          instrument_id: INSTRUMENT,
          timeframe: "1m",
          status: "STALE",
          ts: 1_763_596_800_000,
          stale_since: 1_763_596_790_000,
          reconnect_count: 0,
        },
      }),
    );
    expect(healthMessage?.type).toBe("health");
  });

  it("解析衍生品快照消息", () => {
    const parsed = parseDeskMessage(
      JSON.stringify({
        type: "derivatives",
        data: {
          instrument_id: INSTRUMENT,
          mark_price: "58000.5",
          funding_rate: "0.0001",
          next_funding_ts: 1_763_625_600_000,
          open_interest: "12345.6",
          ts_event: 1_763_596_800_000,
          ts_ingested: 1_763_596_800_000,
        },
      }),
    );
    expect(parsed).toEqual({
      type: "derivatives",
      data: {
        instrument_id: INSTRUMENT,
        mark_price: "58000.5",
        funding_rate: "0.0001",
        next_funding_ts: 1_763_625_600_000,
        open_interest: "12345.6",
        ts_event: 1_763_596_800_000,
        ts_ingested: 1_763_596_800_000,
      },
    });
  });

  it("解析扫描器触发消息并按 trigger_id 字段校验", () => {
    const data = {
      trigger_id: "scn_1",
      instrument_id: INSTRUMENT,
      timeframe: "1m",
      kind: "LIQUIDITY_ANOMALY",
      severity: "WARNING",
      title: "流动性异常",
      summary: "成交额放大",
      tags: ["流动性"],
      created_ts: 1_763_596_800_000,
      snapshot_id: "snap_1",
      route: "LOCAL",
    };
    expect(parseDeskMessage(JSON.stringify({ type: "scanner", data }))).toEqual({
      type: "scanner",
      data,
    });
    expect(
      parseDeskMessage(JSON.stringify({ type: "scanner", data: { summary: "x" } })),
    ).toBeNull();
  });

  it("解析 levels 投影，拒绝缺失 levels 的载荷", () => {
    const parsed = parseDeskMessage(
      JSON.stringify({
        type: "levels",
        data: {
          snapshot_id: "snap",
          instrument_id: INSTRUMENT,
          timeframe: "1m",
          cutoff_ts: 1,
          as_of_ts: 1,
          method_version: "key-levels@1",
          levels: [],
        },
      }),
    );
    expect(parsed?.type).toBe("levels");
    if (parsed?.type === "levels") {
      expect(parsed.data.snapshot_id).toBe("snap");
      expect(parsed.data.levels).toEqual([]);
    }
    expect(
      parseDeskMessage(JSON.stringify({ type: "levels", data: { instrument_id: INSTRUMENT } })),
    ).toBeNull();
  });

  it("解析错误消息", () => {
    const parsed = parseDeskMessage(
      JSON.stringify({
        type: "error",
        code: "INSTRUMENT_NOT_FOUND",
        message: "未知标的 BTC-NOPE",
      }),
    );
    expect(parsed).toEqual({
      type: "error",
      code: "INSTRUMENT_NOT_FOUND",
      message: "未知标的 BTC-NOPE",
    });
  });

  it("忽略无法识别的消息", () => {
    expect(parseDeskMessage("不是 JSON")).toBeNull();
    expect(parseDeskMessage(JSON.stringify({ type: "unknown" }))).toBeNull();
    expect(parseDeskMessage(JSON.stringify({ type: "bar" }))).toBeNull();
    expect(parseDeskMessage(JSON.stringify({ type: "bar", data: {} }))).toBeNull();
  });

  it("解析 alert 触发载荷", () => {
    const parsed = parseDeskMessage(
      JSON.stringify({
        type: "alert",
        data: {
          trigger_id: "trg_1",
          alert_id: "alr_1",
          instrument_id: INSTRUMENT,
          timeframe: "1m",
          condition_kind: "OVERLAY_CROSS",
          triggered_ts: 1_763_596_800_000,
          snapshot_id: "snap-1",
          message: "价格收盘突破水平线 100.5",
        },
      }),
    );
    expect(parsed?.type).toBe("alert");
    if (parsed?.type === "alert") {
      expect(parsed.data.snapshot_id).toBe("snap-1");
      expect(parsed.data.triggered_ts).toBe(1_763_596_800_000);
    }
  });
});
