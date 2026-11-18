import type { FeedHealthEvent, OrderBookEvent } from "@talaria/contracts";

import { formatPrice } from "./format";
import { healthLabel, healthTone, staleDurationMs } from "./health";

interface FeedHealthBadgeProps {
  health: FeedHealthEvent | null;
  now: number;
  live: boolean | null;
}

/** 顶栏健康徽标：实时 / 断流 N 秒 / 恢复中。 */
export function FeedHealthBadge({ health, now, live }: FeedHealthBadgeProps) {
  if (health === null) {
    const tone = live === false ? "offline" : "waiting";
    return (
      <span
        className={`feed-health feed-health--${tone}`}
        data-testid="feed-health"
        data-status={live === false ? "OFFLINE" : "WAITING"}
      >
        {live === false ? "无实时流" : "等待行情"}
      </span>
    );
  }
  return (
    <span
      className={`feed-health feed-health--${healthTone(health.status)}`}
      data-testid="feed-health"
      data-status={health.status}
      aria-live="polite"
    >
      <span className="feed-health-dot" aria-hidden="true" />
      {healthLabel(health, now)}
    </span>
  );
}

interface OrderBookSummaryProps {
  book: OrderBookEvent | null;
  pricePrecision: number;
}

/** 顶栏盘口：只显示最优一档，纵深留给后续订单流任务。 */
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
    </div>
  );
}

interface FeedAlertProps {
  health: FeedHealthEvent | null;
  now: number;
}

/** 断流提示条；仅 STALE 出现，恢复后自动消失。 */
export function FeedAlert({ health, now }: FeedAlertProps) {
  if (health === null || health.status !== "STALE") {
    return null;
  }
  const seconds = Math.floor(staleDurationMs(health, now) / 1000);
  return (
    <div className="feed-alert" role="status" data-testid="feed-alert">
      <strong>实时行情断流</strong>
      <span>
        已持续 {seconds} 秒，正在等待重连与回填；断流期间的 K 线会以
        FEED_GAP 留空，不参与指标计算。
      </span>
    </div>
  );
}
