"""Bybit Linear（USDT 永续）行情连接器。

只使用公开的 Bybit V5 市场接口，无需密钥：K 线、标记价 K 线、资金费率结算
历史与持仓量。错误统一映射为 ``FeedUnavailableError``，由 API 转成 503。
"""

from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Callable, Sequence

import httpx

from talaria.foundation.connector import RawBar
from talaria.foundation.derivatives import DerivativeKind, RawDerivativePoint
from talaria.foundation.errors import FeedUnavailableError
from talaria.foundation.timeframes import timeframe_ms
from talaria.foundation.types import PrecisionTier
from talaria.packs.crypto_perp._proxy import is_loopback_url

DEFAULT_BASE_URL = "https://api.bybit.com"
MAX_LIMIT = 1000
FUNDING_LIMIT = 200
OI_LIMIT = 200

REST_INTERVAL_BY_TIMEFRAME: dict[str, str] = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "1h": "60",
    "4h": "240",
    "1d": "D",
}

OI_INTERVAL_BY_TIMEFRAME: dict[str, str] = {
    "1m": "5min",
    "5m": "5min",
    "15m": "15min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}


class BybitLinearConnector:
    """按 ``end`` 向左分页，返回已闭合的 USDT 永续 K 线。"""

    feed_mode = "bybit"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
        retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": "talaria/0.0.2"},
            trust_env=not is_loopback_url(self._base_url),
        )
        self._owns_client = client is None
        self._retries = max(0, retries)
        self._sleep = sleep
        self._now_ms = now_ms

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch_bars(
        self,
        *,
        symbol: str,
        timeframe: str,
        end_ts: int | None = None,
        limit: int = MAX_LIMIT,
    ) -> Sequence[RawBar]:
        if limit <= 0:
            return []
        limit = min(limit, MAX_LIMIT)
        params: dict[str, str] = {
            "category": "linear",
            "symbol": symbol,
            "interval": REST_INTERVAL_BY_TIMEFRAME[timeframe],
            "limit": str(limit),
        }
        if end_ts is not None:
            params["end"] = str(end_ts)

        payload = self._request("/v5/market/kline", params)
        self._check(payload)

        interval = timeframe_ms(timeframe)
        now = self._now_ms()
        bars: list[RawBar] = []
        for row in payload.get("result", {}).get("list", []):
            try:
                ts_event = int(row[0])
                if ts_event + interval > now:
                    continue  # 排除未闭合的当前柱
                bars.append(
                    RawBar(
                        symbol=symbol,
                        timeframe=timeframe,
                        ts_event=ts_event,
                        open=Decimal(row[1]),
                        high=Decimal(row[2]),
                        low=Decimal(row[3]),
                        close=Decimal(row[4]),
                        volume=Decimal(row[5]),
                        turnover=Decimal(row[6]),
                        ts_available=ts_event + interval,
                        ts_ingested=now,
                        ts_committed=now,
                        precision=PrecisionTier.SECOND_VERIFIED,
                    )
                )
            except (IndexError, ValueError, InvalidOperation) as exc:
                raise FeedUnavailableError("Bybit K 线数据格式错误") from exc
        bars.sort(key=lambda bar: bar.ts_event)
        return bars

    def fetch_mark_price_bars(
        self,
        *,
        symbol: str,
        timeframe: str,
        end_ts: int | None = None,
        limit: int = MAX_LIMIT,
    ) -> Sequence[RawDerivativePoint]:
        """标记价 K 线：Bybit 返回 ``[start, open, high, low, close]``。"""
        if limit <= 0:
            return []
        params: dict[str, str] = {
            "category": "linear",
            "symbol": symbol,
            "interval": REST_INTERVAL_BY_TIMEFRAME[timeframe],
            "limit": str(min(limit, MAX_LIMIT)),
        }
        if end_ts is not None:
            params["end"] = str(end_ts)
        payload = self._request("/v5/market/mark-price-kline", params)
        self._check(payload)
        interval = timeframe_ms(timeframe)
        now = self._now_ms()
        points: list[RawDerivativePoint] = []
        for row in payload.get("result", {}).get("list", []):
            try:
                ts_event = int(row[0])
                if ts_event + interval > now:
                    continue
                points.append(
                    RawDerivativePoint(
                        kind=DerivativeKind.MARK_PRICE,
                        symbol=symbol,
                        timeframe=timeframe,
                        ts_event=ts_event,
                        value=Decimal(row[4]),
                        ts_available=ts_event + interval,
                        ts_ingested=now,
                        ts_committed=now,
                        precision=PrecisionTier.SECOND_VERIFIED,
                    )
                )
            except (IndexError, ValueError, InvalidOperation) as exc:
                raise FeedUnavailableError("Bybit 标记价 K 线数据格式错误") from exc
        points.sort(key=lambda point: point.ts_event)
        return points

    def fetch_funding_settlements(
        self,
        *,
        symbol: str,
        end_ts: int | None = None,
        limit: int = FUNDING_LIMIT,
    ) -> Sequence[RawDerivativePoint]:
        """资金费率结算历史；Bybit 用 ``endTime`` 向左翻页。"""
        if limit <= 0:
            return []
        params: dict[str, str] = {
            "category": "linear",
            "symbol": symbol,
            "limit": str(min(limit, FUNDING_LIMIT)),
        }
        if end_ts is not None:
            params["endTime"] = str(end_ts)
        payload = self._request("/v5/market/funding/history", params)
        self._check(payload)
        now = self._now_ms()
        points: list[RawDerivativePoint] = []
        for row in payload.get("result", {}).get("list", []):
            try:
                ts_event = int(row["fundingRateTimestamp"])
                if ts_event > now:
                    continue
                points.append(
                    RawDerivativePoint(
                        kind=DerivativeKind.FUNDING_RATE,
                        symbol=symbol,
                        timeframe=None,
                        ts_event=ts_event,
                        value=Decimal(row["fundingRate"]),
                        ts_published=ts_event,
                        ts_available=ts_event,
                        ts_ingested=now,
                        ts_committed=now,
                        precision=PrecisionTier.SECOND_VERIFIED,
                    )
                )
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise FeedUnavailableError("Bybit 资金费率数据格式错误") from exc
        points.sort(key=lambda point: point.ts_event)
        return points

    def fetch_open_interest(
        self,
        *,
        symbol: str,
        timeframe: str,
        end_ts: int | None = None,
        limit: int = OI_LIMIT,
    ) -> Sequence[RawDerivativePoint]:
        """持仓量序列（Bybit 最小 5 分钟）；用 ``endTime`` 向左翻页。"""
        if limit <= 0:
            return []
        interval_time = OI_INTERVAL_BY_TIMEFRAME.get(timeframe)
        if interval_time is None:
            raise FeedUnavailableError(f"Bybit 持仓量不支持周期 {timeframe}")
        params: dict[str, str] = {
            "category": "linear",
            "symbol": symbol,
            "intervalTime": interval_time,
            "limit": str(min(limit, OI_LIMIT)),
        }
        if end_ts is not None:
            params["endTime"] = str(end_ts)
        payload = self._request("/v5/market/open-interest", params)
        self._check(payload)
        interval = timeframe_ms(timeframe)
        now = self._now_ms()
        points: list[RawDerivativePoint] = []
        for row in payload.get("result", {}).get("list", []):
            try:
                ts_event = int(row["timestamp"])
                if ts_event + interval > now:
                    continue
                points.append(
                    RawDerivativePoint(
                        kind=DerivativeKind.OPEN_INTEREST,
                        symbol=symbol,
                        timeframe=timeframe,
                        ts_event=ts_event,
                        value=Decimal(row["openInterest"]),
                        ts_available=ts_event + interval,
                        ts_ingested=now,
                        ts_committed=now,
                        precision=PrecisionTier.SECOND_VERIFIED,
                    )
                )
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise FeedUnavailableError("Bybit 持仓量数据格式错误") from exc
        points.sort(key=lambda point: point.ts_event)
        return points

    @staticmethod
    def _check(payload: dict) -> None:
        if payload.get("retCode") != 0:
            raise FeedUnavailableError(
                f"Bybit 返回错误 {payload.get('retCode')}: {payload.get('retMsg')}"
            )

    def _request(self, path: str, params: dict[str, str]) -> dict:
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                response = self._client.get(
                    f"{self._base_url}{path}", params=params
                )
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < self._retries:
                    self._sleep(0.2 * (2**attempt))
        raise FeedUnavailableError(f"Bybit 行情不可用：{last_error}") from last_error
