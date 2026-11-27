import type {
  AlertTriggerRecord,
  DerivativeTickEvent,
  FeedHealthEvent,
  KeyLevelSet,
  KlineEvent,
  OrderBookEvent,
  PaperAccountView,
  ScannerTrigger,
  StructureSet,
} from "@talaria/contracts";

import { t } from "../../lib/i18n";

export type DeskMessage =
  | { type: "hello"; feed_mode: string; server_ts: number; live: boolean; denied_alerts?: string[] }
  | { type: "channel_mode"; instrument_id: string; timeframe: string; realtime: boolean }
  | { type: "alert_quota"; denied_alerts: string[] }
  | { type: "bar"; data: KlineEvent }
  | { type: "book"; data: OrderBookEvent }
  | { type: "health"; data: FeedHealthEvent }
  | { type: "derivatives"; data: DerivativeTickEvent }
  | { type: "levels"; data: KeyLevelSet }
  | { type: "structures"; data: StructureSet }
  | { type: "alert"; data: AlertTriggerRecord }
  | { type: "scanner"; data: ScannerTrigger }
  | { type: "paper_account"; data: PaperAccountView }
  | {
      type: "error";
      code: string;
      message: string;
      owner?: string;
      instrument_id?: string;
      timeframe?: string;
    };

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

export function parseDeskMessage(raw: string): DeskMessage | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  const record = asRecord(parsed);
  if (record === null) return null;

  const denied =
    Array.isArray(record.denied_alerts) &&
    record.denied_alerts.every((id) => typeof id === "string")
      ? (record.denied_alerts as string[])
      : null;

  switch (record.type) {
    case "alert_quota":
      return denied === null ? null : { type: "alert_quota", denied_alerts: denied };
    case "hello":
      return {
        type: "hello",
        feed_mode: typeof record.feed_mode === "string" ? record.feed_mode : "unknown",
        server_ts: typeof record.server_ts === "number" ? record.server_ts : 0,
        live: record.live === true,
        ...(denied !== null && { denied_alerts: denied }),
      };
    case "channel_mode":
      return typeof record.instrument_id === "string" &&
        typeof record.timeframe === "string" &&
        typeof record.realtime === "boolean"
        ? {
            type: "channel_mode",
            instrument_id: record.instrument_id,
            timeframe: record.timeframe,
            realtime: record.realtime,
          }
        : null;
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
    case "structures": {
      const data = asRecord(record.data);
      return data !== null &&
        typeof data.instrument_id === "string" &&
        typeof data.timeframe === "string" &&
        Array.isArray(data.structures)
        ? { type: "structures", data: record.data as StructureSet }
        : null;
    }
    case "alert": {
      const data = asRecord(record.data);
      return data !== null &&
        typeof data.alert_id === "string" &&
        typeof data.instrument_id === "string" &&
        typeof data.triggered_ts === "number"
        ? { type: "alert", data: record.data as AlertTriggerRecord }
        : null;
    }
    case "scanner": {
      const data = asRecord(record.data);
      return data !== null &&
        typeof data.trigger_id === "string" &&
        typeof data.instrument_id === "string" &&
        typeof data.kind === "string" &&
        typeof data.created_ts === "number" &&
        typeof data.snapshot_id === "string"
        ? { type: "scanner", data: record.data as ScannerTrigger }
        : null;
    }
    case "paper_account": {
      const data = asRecord(record.data);
      return data !== null &&
        typeof data.sub_account_id === "string" &&
        Array.isArray(data.positions) &&
        Array.isArray(data.rounds) &&
        Array.isArray(data.fills)
        ? { type: "paper_account", data: record.data as PaperAccountView }
        : null;
    }
    case "error":
      return {
        type: "error",
        ...(typeof record.owner === "string" && { owner: record.owner }),
        ...(typeof record.instrument_id === "string" && {
          instrument_id: record.instrument_id,
        }),
        ...(typeof record.timeframe === "string" && { timeframe: record.timeframe }),
        code: typeof record.code === "string" ? record.code : "INTERNAL",
        message:
          typeof record.message === "string"
            ? record.message
            : t("未知错误", "Unknown error"),
      };
    default:
      return null;
  }
}
