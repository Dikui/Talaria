import type { FeedHealthEvent, OrderBookEvent } from "@talaria/contracts";

import { formatPrice } from "./format";
import { feedAlertView, feedBadgeView } from "./health";
import type { ConnectionState } from "./reducer";

interface FeedHealthBadgeProps {
  health: FeedHealthEvent | null;
  now: number;
  live: boolean | null;
  connection: ConnectionState;
}

/** 顶栏健康徽标：实时 / 断流 N 秒 / 恢复中 / 连接断开 · 重连中。 */
export function FeedHealthBadge({
  health,
  now,
  live,
  connection,
}: FeedHealthBadgeProps) {
  const view = feedBadgeView(health, now, live, connection);
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

/** 顶栏盘口：最优一档 + 买卖价差，纵深留给后续订单流任务。 */
export function OrderBookSummary({ book, pricePrecision }: OrderBookSummaryProps) {
  if (book === null) {
    return (
      <div
        className="order-book order-book--empty"
        data-testid="order-book"
        data-ready="false"
      >
        <span className="order-book-title">盘口</span>
        <span className="order-book-placeholder">等待盘口</span>
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
      <span className="order-book-title">盘口</span>
      <span className="order-book-row order-book-row--ask">
        <span className="order-book-label">卖一</span>
        <span className="order-book-price">
          {formatPrice(book.ask_price, pricePrecision)}
        </span>
      </span>
      <span className="order-book-row order-book-row--bid">
        <span className="order-book-label">买一</span>
        <span className="order-book-price">
          {formatPrice(book.bid_price, pricePrecision)}
        </span>
      </span>
      <span className="order-book-row order-book-row--spread">
        <span className="order-book-label">价差</span>
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
}

/** 提示条：本地连接断开或行情断流时出现，恢复后自动消失。 */
export function FeedAlert({
  health,
  now,
  live,
  connection,
}: FeedAlertProps) {
  const view = feedAlertView(health, now, live, connection);
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
