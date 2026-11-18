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
          status: "STALE",
          ts: 1_763_596_800_000,
          stale_since: 1_763_596_790_000,
          reconnect_count: 0,
        },
      }),
    );
    expect(healthMessage?.type).toBe("health");
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
});
