import type { WatchlistGroup, WatchlistItem } from "@talaria/contracts";

import { getLocale, type Locale } from "../../lib/i18n";
import { formatPrice } from "../feed/format";

export const WATCHLIST_COMPACT_MAX_WIDTH = 720;

export interface QuoteView {
  price: string;
  change: string;
  changeTone: "up" | "down" | "flat";
  realtimeLabel: string;
  degradationLabel: string | null;
  statusLabel: string;
  staleLabel: string | null;
  sourceLabel: string;
  title: string;
}

export function moveId(ids: readonly string[], id: string, direction: -1 | 1): string[] {
  const next = [...ids];
  const index = next.indexOf(id);
  const target = index + direction;
  if (index < 0 || target < 0 || target >= next.length) return next;
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

export function isEmptyWatchlist(
  groups: readonly Pick<WatchlistGroup, "items">[] | null,
): boolean {
  return groups !== null && groups.length === 0;
}

export function isInstrumentSelected(
  instrumentId: string,
  selectedInstrumentId: string,
): boolean {
  return instrumentId === selectedInstrumentId;
}

export function canEditGroup(
  viewportWidth: number,
  group: Pick<WatchlistGroup, "is_system" | "kind">,
): boolean {
  return (
    viewportWidth > WATCHLIST_COMPACT_MAX_WIDTH &&
    group.kind === "MANUAL" &&
    !group.is_system
  );
}

export function quoteView(
  item: WatchlistItem,
  pricePrecision: number,
  locale: Locale = getLocale(),
): QuoteView {
  const price =
    item.latest_price === null || item.latest_price === undefined
      ? "—"
      : formatPrice(item.latest_price, pricePrecision);
  const numericChange =
    item.change_pct === null || item.change_pct === undefined
      ? null
      : Number(item.change_pct);
  const change =
    numericChange === null || !Number.isFinite(numericChange)
      ? "—"
      : `${numericChange > 0 ? "+" : ""}${numericChange.toFixed(2)}%`;
  const changeTone =
    numericChange === null || !Number.isFinite(numericChange)
      ? "flat"
      : numericChange > 0
        ? "up"
        : numericChange < 0
          ? "down"
          : "flat";

  const realtimeLabel = item.realtime
    ? locale === "zh"
      ? "实时"
      : "Live"
    : locale === "zh"
      ? "非实时"
      : "Not live";
  const degradationLabel =
    item.degradation_reason === "QUOTA"
      ? locale === "zh"
        ? "配额降级"
        : "Quota"
      : item.degradation_reason === "NO_FEED"
        ? locale === "zh"
          ? "无实时源"
          : "No feed"
        : null;
  const staleLabel = item.stale ? "STALE" : null;
  const sourceLabel =
    item.quote_source === "SUPERVISOR"
      ? locale === "zh"
        ? "实时流"
        : "Stream"
      : item.quote_source === "SNAPSHOT"
        ? locale === "zh"
          ? "快照"
          : "Snapshot"
        : locale === "zh"
          ? "无报价"
          : "None";
  const statusLabel = degradationLabel ?? realtimeLabel;

  return {
    price,
    change,
    changeTone,
    realtimeLabel,
    degradationLabel,
    statusLabel,
    staleLabel,
    sourceLabel,
    title: `${item.instrument_id} · ${price} · ${change} · ${statusLabel} · ${sourceLabel}`,
  };
}
