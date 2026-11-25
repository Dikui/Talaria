import type { FeedHealthEvent, OrderBookEvent } from "@talaria/contracts";

import { t, useLocale } from "../../lib/i18n";
import { formatPrice } from "./format";
import { feedAlertView, feedBadgeView } from "./health";
import type { ConnectionState } from "./reducer";

interface FeedHealthBadgeProps {
  health: FeedHealthEvent | null;
  now: number;
  live: boolean | null;
  connection: ConnectionState;
  channelRealtime?: boolean | null;
}

export function FeedHealthBadge({
  health,
  now,
  live,
  connection,
  channelRealtime,
}: FeedHealthBadgeProps) {
  const locale = useLocale();
  const view = feedBadgeView(health, now, live, connection, locale, channelRealtime);
  return (
    <span
      className={`feed-health feed-health--${view.tone}`}
      data-testid="feed-health"
      data-status={view.status}
      aria-live="polite"
    >
      <span className="feed-health-dot" aria-hidden="true" />
      {view.label}
    </span>
  );
}

interface OrderBookSummaryProps {
  book: OrderBookEvent | null;
  pricePrecision: number;
}

export function OrderBookSummary({ book, pricePrecision }: OrderBookSummaryProps) {
  useLocale();
  if (book === null) {
    return (
      <div
        className="order-book order-book--empty"
        data-testid="order-book"
        data-ready="false"
      >
        <span className="order-book-title">{t("盘口", "Book")}</span>
        <span className="order-book-placeholder">{t("等待", "—")}</span>
      </div>
    );
  }
  const ask = Number(book.ask_price);
  const bid = Number(book.bid_price);
  const spread =
    Number.isFinite(ask) && Number.isFinite(bid)
      ? formatPrice(String(ask - bid), pricePrecision)
      : "—";
  return (
    <div className="order-book" data-testid="order-book" data-ready="true">
      <span className="order-book-row order-book-row--ask">
        <span className="order-book-label">{t("卖", "A")}</span>
        <span className="order-book-price">{formatPrice(book.ask_price, pricePrecision)}</span>
      </span>
      <span className="order-book-row order-book-row--bid">
        <span className="order-book-label">{t("买", "B")}</span>
        <span className="order-book-price">{formatPrice(book.bid_price, pricePrecision)}</span>
      </span>
      <span className="order-book-row order-book-row--spread">
        <span className="order-book-label">{t("差", "Spr")}</span>
        <span className="order-book-price" data-testid="order-book-spread">
          {spread}
        </span>
      </span>
    </div>
  );
}

interface FeedAlertProps {
  health: FeedHealthEvent | null;
  now: number;
  live: boolean | null;
  connection: ConnectionState;
  channelRealtime?: boolean | null;
}

export function FeedAlert({
  health,
  now,
  live,
  connection,
  channelRealtime,
}: FeedAlertProps) {
  const locale = useLocale();
  const view = feedAlertView(health, now, live, connection, locale, channelRealtime);
  if (view === null) {
    return null;
  }
  return (
    <div className="feed-alert" role="status" data-testid="feed-alert">
      <strong>{view.title}</strong>
      <span>{view.detail}</span>
    </div>
  );
}
