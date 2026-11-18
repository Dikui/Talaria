import { useEffect, useState } from "react";

import { formatPrice } from "../feed/format";

interface DerivativeSummaryProps {
  markPrice: string | null;
  fundingRate: string | null;
  nextFundingTs: number | null;
  openInterest: string | null;
  pricePrecision: number;
  hasOiHistory: boolean;
}

function fundingText(value: string | null): string {
  if (value === null) {
    return "—";
  }
  const rate = Number(value);
  if (!Number.isFinite(rate)) {
    return "—";
  }
  const percent = (rate * 100).toFixed(4);
  return `${rate > 0 ? "+" : ""}${percent}%`;
}

function countdownText(nextFundingTs: number | null, now: number): string {
  if (nextFundingTs === null) {
    return "—";
  }
  const remaining = Math.max(0, nextFundingTs - now);
  const totalSeconds = Math.floor(remaining / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return [hours, minutes, seconds]
    .map((value) => String(value).padStart(2, "0"))
    .join(":");
}

function oiText(value: string | null, hasOiHistory: boolean): string {
  if (value === null) {
    return hasOiHistory ? "—" : "暂无 OI 历史";
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return "—";
  }
  return parsed.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

/** 顶栏衍生品状态：只渲染后端读数与本地倒计时，不做任何指标数学。 */
export function DerivativeSummary({
  markPrice,
  fundingRate,
  nextFundingTs,
  openInterest,
  pricePrecision,
  hasOiHistory,
}: DerivativeSummaryProps) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const emptyOi = openInterest === null && !hasOiHistory;
  return (
    <div
      className={`derivative-summary${emptyOi ? " is-empty-oi" : ""}`}
      data-testid="derivative-summary"
      aria-label="衍生品实时状态"
    >
      <span className="derivative-item">
        <span className="derivative-label">标记价</span>
        <strong className="derivative-value" data-testid="derivative-mark-price">
          {markPrice === null ? "—" : formatPrice(markPrice, pricePrecision)}
        </strong>
      </span>
      <span className="derivative-item">
        <span className="derivative-label">资金费率</span>
        <strong
          className={`derivative-value${
            fundingRate !== null && Number(fundingRate) < 0 ? " is-negative" : ""
          }`}
          data-testid="derivative-funding-rate"
        >
          {fundingText(fundingRate)}
        </strong>
      </span>
      <span className="derivative-item derivative-item--optional">
        <span className="derivative-label">下次结算</span>
        <strong className="derivative-value" data-testid="derivative-countdown">
          {countdownText(nextFundingTs, now)}
        </strong>
      </span>
      <span className="derivative-item derivative-item--optional">
        <span className="derivative-label">持仓量</span>
        <strong
          className={`derivative-value${emptyOi ? " is-muted" : ""}`}
          data-testid="derivative-open-interest"
        >
          {oiText(openInterest, hasOiHistory)}
        </strong>
      </span>
    </div>
  );
}
