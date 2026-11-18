import type {
  DerivativeTickEvent,
  FeedHealthEvent,
  KeyLevelSet,
  KlineEvent,
  OrderBookEvent,
} from "@talaria/contracts";

/**
 * 快通道 `WS /ws/desk` 的服务端消息。
 *
 * 服务端以 `{"type": "...", "data": ...}` 推送增量；hello 与 error 不走 data 字段。
 * 这里只做结构校验，业务字段由契约类型保证（服务端由同一份 JSON Schema 生成）。
 */
export type DeskMessage =
  | { type: "hello"; feed_mode: string; server_ts: number; live: boolean }
  | { type: "bar"; data: KlineEvent }
  | { type: "book"; data: OrderBookEvent }
  | { type: "health"; data: FeedHealthEvent }
  | { type: "derivatives"; data: DerivativeTickEvent }
  | { type: "levels"; data: KeyLevelSet }
  | { type: "error"; code: string; message: string };

type RawRecord = Record<string, unknown>;

function asRecord(value: unknown): RawRecord | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  return value as RawRecord;
}

function hasInstrumentId(value: unknown): boolean {
  const record = asRecord(value);
  return record !== null && typeof record.instrument_id === "string";
}

/** 解析一条 WS 文本帧；结构不认识时返回 null，调用方忽略即可。 */
export function parseDeskMessage(raw: string): DeskMessage | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  const record = asRecord(parsed);
  if (record === null) {
    return null;
  }
  switch (record.type) {
    case "hello":
      return {
        type: "hello",
        feed_mode:
          typeof record.feed_mode === "string" ? record.feed_mode : "unknown",
        server_ts:
          typeof record.server_ts === "number" ? record.server_ts : 0,
        live: record.live === true,
      };
    case "bar":
      return hasInstrumentId(record.data)
        ? { type: "bar", data: record.data as KlineEvent }
        : null;
    case "book":
      return hasInstrumentId(record.data)
        ? { type: "book", data: record.data as OrderBookEvent }
        : null;
    case "health":
      return hasInstrumentId(record.data)
        ? { type: "health", data: record.data as FeedHealthEvent }
        : null;
    case "derivatives":
      return hasInstrumentId(record.data)
        ? { type: "derivatives", data: record.data as DerivativeTickEvent }
        : null;
    case "levels": {
      const data = asRecord(record.data);
      return data !== null &&
        typeof data.instrument_id === "string" &&
        typeof data.timeframe === "string" &&
        Array.isArray(data.levels)
        ? { type: "levels", data: record.data as KeyLevelSet }
        : null;
    }
    case "error":
      return {
        type: "error",
        code: typeof record.code === "string" ? record.code : "INTERNAL",
        message:
          typeof record.message === "string" ? record.message : "未知错误",
      };
    default:
      return null;
  }
}
