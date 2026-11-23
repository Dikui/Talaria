import { getLocale, t } from "./i18n";

// Service messages can include diagnostic data (e.g. a price or instrument ID).
// Translate the meaning of the stable error code; never discard that detail.
const summaries: Readonly<Record<string, string>> = {
  UNAUTHORIZED: "Refresh the workbench to renew your session.",
  VALIDATION_ERROR: "Check the request parameters.",
  DRAFT_CONFLICT: "The draft has changed. Review its latest version.",
  BARS_RANGE_TOO_LARGE: "Select a shorter candle range.",
  BARS_BACKFILL_TIMEOUT: "Historical candle backfill timed out.",
  SNAPSHOT_MISMATCH: "The evidence does not match the snapshot.",
  EVIDENCE_EMPTY: "Evidence is required.", EVIDENCE_UNRESOLVED: "The evidence could not be resolved.",
  PRICE_INVALID: "The price is invalid.", PRICE_NOT_TICK_ALIGNED: "Use a price aligned with the tick size.",
  PRICE_OUT_OF_BOUNDS: "The price is outside the allowed range.",
  TIME_BEFORE_SNAPSHOT: "The time precedes the snapshot.", TIME_TOO_FAR_FUTURE: "The time is too far in the future.",
  FEED_UNAVAILABLE: "Market data is unavailable.", NOT_IMPLEMENTED: "This operation is not implemented.",
  FEATURE_DISABLED: "This feature is disabled.", SERVICE_LOCKED: "Another service is using the data directory.",
};

export function serviceErrorMessage(status: number, code: string, detail?: string): string {
  const fallback = t(`请求失败（HTTP ${status}）`, `Request failed (HTTP ${status})`);
  if (!detail) return fallback;
  if (getLocale() === "zh" || !/\p{Script=Han}/u.test(detail)) return detail;
  const summary = Object.hasOwn(summaries, code) ? summaries[code]
    : status === 404 ? "The requested resource was not found." : fallback;
  return `${summary} · ${detail}`;
}
