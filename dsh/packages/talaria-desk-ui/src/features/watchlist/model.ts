import type { WatchlistGroup, WatchlistItem } from "@talaria/contracts";

import { getLocale, type Locale } from "../../lib/i18n";
import { formatPrice } from "../feed/format";

/** 与 styles.css 中隐藏编辑控件的断点保持一致。 */
export const WATCHLIST_COMPACT_MAX_WIDTH = 720;

export interface WatchlistGroupView {
  group_id: string;
  name: string;
  kind: WatchlistGroup["kind"];
  is_system: boolean;
  items: WatchlistItem[];
}

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

/** 把 id 在列表中上移/下移一格；越界或不存在时返回原顺序的副本。 */
export function moveId(
  ids: readonly string[],
  id: string,
  direction: -1 | 1,
): string[] {
  const next = [...ids];
  const index = next.indexOf(id);
  const target = index + direction;
  if (index < 0 || target < 0 || target >= next.length) {
    return next;
  }
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

/** 分组列表为空（而不是尚未加载）时展示空态引导。 */
export function isEmptyWatchlist(
  groups: readonly Pick<WatchlistGroup, "items">[] | null,
): boolean {
  return groups !== null && groups.length === 0;
}

/** 点击标的后的选中判定；列表与图表都只认当前选中标识。 */
export function isInstrumentSelected(
  instrumentId: string,
  selectedInstrumentId: string,
): boolean {
  return instrumentId === selectedInstrumentId;
}

/**
 * 移动端只读：390 宽度下仍可浏览和点击切换，但分组/条目的编辑入口隐藏。
 * 系统动态分组（SCANNER_FOCUS）在任何宽度都只读。
 */
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

  const realtimeLabel = item.realtime ? (locale === "zh" ? "实时" : "Live") : (locale === "zh" ? "非实时" : "Not live");
  const degradationLabel =
    item.degradation_reason === "QUOTA"
      ? locale === "zh" ? "配额降级" : "Quota downgraded"
      : item.degradation_reason === "NO_FEED"
        ? locale === "zh" ? "无实时源" : "No live source"
        : null;
  const staleLabel = item.stale ? "STALE" : null;
  const sourceLabel =
    item.quote_source === "SUPERVISOR"
      ? locale === "zh" ? "实时流" : "Live stream"
      : item.quote_source === "SNAPSHOT"
        ? locale === "zh" ? "快照" : "Snapshot"
        : locale === "zh" ? "无报价" : "No quote";
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
