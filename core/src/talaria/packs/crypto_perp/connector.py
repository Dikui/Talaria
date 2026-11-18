"""Bybit Linear（USDT 永续）行情连接器。

只使用公开的 ``/v5/market/kline``，无需密钥；错误统一映射为
``FeedUnavailableError``，由 API 转成 503。
"""

from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Callable, Sequence

import httpx

from talaria.foundation.connector import RawBar
from talaria.foundation.errors import FeedUnavailableError
from talaria.foundation.timeframes import timeframe_ms
from talaria.foundation.types import PrecisionTier

DEFAULT_BASE_URL = "https://api.bybit.com"
MAX_LIMIT = 1000


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
        # 不继承环境代理：避免意外的 HTTP(S)_PROXY/NO_PROXY 影响本地或 API 流量；
        # 需要代理的部署可显式注入已配置的 httpx.Client。
        self._client = client or httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": "talaria/0.0.2"},
            trust_env=False,
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
            "interval": timeframe,
            "limit": str(limit),
        }
        if end_ts is not None:
            params["end"] = str(end_ts)

        payload = self._request(params)
        if payload.get("retCode") != 0:
            raise FeedUnavailableError(
                f"Bybit 返回错误 {payload.get('retCode')}: {payload.get('retMsg')}"
            )

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

    def _request(self, params: dict[str, str]) -> dict:
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                response = self._client.get(
                    f"{self._base_url}/v5/market/kline", params=params
                )
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < self._retries:
                    self._sleep(0.2 * (2**attempt))
        raise FeedUnavailableError(f"Bybit 行情不可用：{last_error}") from last_error
