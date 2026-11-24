import { useEffect, useState } from "react";

import { getLocale, t, useLocale } from "../../lib/i18n";
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
  if (value === null) return "—";
  const rate = Number(value);
  if (!Number.isFinite(rate)) return "—";
  return `${rate > 0 ? "+" : ""}${(rate * 100).toFixed(4)}%`;
}

function countdownText(nextFundingTs: number | null, now: number): string {
  if (nextFundingTs === null) return "—";
  const remaining = Math.max(0, nextFundingTs - now);
  const totalSeconds = Math.floor(remaining / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return [hours, minutes, seconds].map((v) => String(v).padStart(2, "0")).join(":");
}

function oiText(value: string | null, hasOiHistory: boolean): string {
  if (value === null) return hasOiHistory ? "—" : t("无 OI", "No OI");
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "—";
  return parsed.toLocaleString(getLocale() === "zh" ? "zh-CN" : "en-US", {
    maximumFractionDigits: 2,
  });
}

export function DerivativeSummary({
  markPrice,
  fundingRate,
  nextFundingTs,
  openInterest,
  pricePrecision,
  hasOiHistory,
}: DerivativeSummaryProps) {
  const [now, setNow] = useState(() => Date.now());
  useLocale();

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const emptyOi = openInterest === null && !hasOiHistory;
  return (
    <div
      className={`derivative-summary${emptyOi ? " is-empty-oi" : ""}`}
      data-testid="derivative-summary"
      aria-label={t("衍生品实时状态", "Live derivatives")}
    >
      <span className="derivative-item">
        <span className="derivative-label">{t("标记", "Mark")}</span>
        <strong className="derivative-value" data-testid="derivative-mark-price">
          {markPrice === null ? "—" : formatPrice(markPrice, pricePrecision)}
        </strong>
      </span>
      <span className="derivative-item">
        <span className="derivative-label">{t("费率", "Funding")}</span>
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
        <span className="derivative-label">{t("结算", "Settle")}</span>
        <strong className="derivative-value" data-testid="derivative-countdown">
          {countdownText(nextFundingTs, now)}
        </strong>
      </span>
      <span className="derivative-item derivative-item--optional">
        <span className="derivative-label">OI</span>
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
