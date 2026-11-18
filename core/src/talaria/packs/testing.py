"""离线确定性行情连接器。

读取 ``tests/fixtures/bybit/<SYMBOL>/<timeframe>.json`` 的 Bybit 原始响应。
它放在 ``packs`` 层而不是 ``foundation``/``perception``：核心层不得知道任何具体的
数据来源或测试装置。
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Sequence

from talaria.foundation.connector import RawBar
from talaria.foundation.derivatives import DerivativeKind, RawDerivativePoint
from talaria.foundation.errors import FeedUnavailableError
from talaria.foundation.timeframes import timeframe_ms
from talaria.foundation.types import PrecisionTier


class FixtureConnector:
    """按 Bybit 相同的「按 end 向左分页」语义读取本地 fixture。

    衍生品文件放在 ``<SYMBOL>/derivatives/``：``mark_price_<tf>.json``、
    ``funding.json``、``open_interest_<tf>.json``，均为 Bybit 原始响应外壳。
    标记价缺专用文件时回退用同周期 K 线收盘价，保证离线样本最小可用。
    """

    feed_mode = "fixture"

    def __init__(self, fixture_dir: Path | str) -> None:
        self._fixture_dir = Path(fixture_dir)

    def fetch_bars(
        self,
        *,
        symbol: str,
        timeframe: str,
        end_ts: int | None = None,
        limit: int = 1000,
    ) -> Sequence[RawBar]:
        if limit <= 0:
            return []
        limit = min(limit, 1000)
        path = self._fixture_dir / symbol / f"{timeframe}.json"
        if not path.is_file():
            raise FeedUnavailableError(f"离线样本缺少 {symbol} {timeframe}（{path}）")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FeedUnavailableError(f"离线样本无法读取：{path}") from exc
        if payload.get("retCode") != 0:
            raise FeedUnavailableError(
                f"离线样本返回错误 {payload.get('retCode')}: {payload.get('retMsg')}"
            )

        interval = timeframe_ms(timeframe)
        rows = payload.get("result", {}).get("list", [])
        # Bybit 原始响应按开盘时间降序；取 end 之前最新的 limit 根，再翻转成升序。
        selected: list[RawBar] = []
        for row in rows:
            try:
                ts_event = int(row[0])
                if end_ts is not None and ts_event > end_ts:
                    continue
                selected.append(
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
                        ts_ingested=ts_event + interval,
                        ts_committed=ts_event + interval,
                        precision=PrecisionTier.SECOND_VERIFIED,
                    )
                )
            except (IndexError, ValueError, InvalidOperation) as exc:
                raise FeedUnavailableError(f"离线样本数据格式错误：{path}") from exc
            if len(selected) >= limit:
                break
        selected.reverse()
        return selected

    def fetch_mark_price_bars(
        self,
        *,
        symbol: str,
        timeframe: str,
        end_ts: int | None = None,
        limit: int = 1000,
    ) -> Sequence[RawDerivativePoint]:
        if limit <= 0:
            return []
        limit = min(limit, 1000)
        path = self._fixture_dir / symbol / "derivatives" / f"mark_price_{timeframe}.json"
        if path.is_file():
            rows = self._rows(path)
            price_at = lambda row: Decimal(row[4])  # noqa: E731
        else:
            fallback = self._fixture_dir / symbol / f"{timeframe}.json"
            if not fallback.is_file():
                raise FeedUnavailableError(
                    f"离线样本缺少 {symbol} 标记价 {timeframe}（{path}）"
                )
            rows = self._rows(fallback)
            price_at = lambda row: Decimal(row[4])  # noqa: E731
        interval = timeframe_ms(timeframe)
        selected: list[RawDerivativePoint] = []
        for row in rows:
            try:
                ts_event = int(row[0])
                if end_ts is not None and ts_event > end_ts:
                    continue
                selected.append(
                    RawDerivativePoint(
                        kind=DerivativeKind.MARK_PRICE,
                        symbol=symbol,
                        timeframe=timeframe,
                        ts_event=ts_event,
                        value=price_at(row),
                        ts_available=ts_event + interval,
                        ts_ingested=ts_event + interval,
                        ts_committed=ts_event + interval,
                        precision=PrecisionTier.SECOND_VERIFIED,
                    )
                )
            except (IndexError, ValueError, InvalidOperation) as exc:
                raise FeedUnavailableError(f"离线标记价数据格式错误：{path}") from exc
            if len(selected) >= limit:
                break
        selected.reverse()
        return selected

    def fetch_funding_settlements(
        self,
        *,
        symbol: str,
        end_ts: int | None = None,
        limit: int = 200,
    ) -> Sequence[RawDerivativePoint]:
        if limit <= 0:
            return []
        path = self._fixture_dir / symbol / "derivatives" / "funding.json"
        selected: list[RawDerivativePoint] = []
        for row in self._rows(path):
            try:
                ts_event = int(row["fundingRateTimestamp"])
                if end_ts is not None and ts_event > end_ts:
                    continue
                selected.append(
                    RawDerivativePoint(
                        kind=DerivativeKind.FUNDING_RATE,
                        symbol=symbol,
                        timeframe=None,
                        ts_event=ts_event,
                        value=Decimal(row["fundingRate"]),
                        ts_published=ts_event,
                        ts_available=ts_event,
                        ts_ingested=ts_event,
                        ts_committed=ts_event,
                        precision=PrecisionTier.SECOND_VERIFIED,
                    )
                )
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise FeedUnavailableError(f"离线资金费率数据格式错误：{path}") from exc
            if len(selected) >= limit:
                break
        selected.reverse()
        return selected

    def fetch_open_interest(
        self,
        *,
        symbol: str,
        timeframe: str,
        end_ts: int | None = None,
        limit: int = 200,
    ) -> Sequence[RawDerivativePoint]:
        if limit <= 0:
            return []
        path = self._fixture_dir / symbol / "derivatives" / f"open_interest_{timeframe}.json"
        interval = timeframe_ms(timeframe)
        selected: list[RawDerivativePoint] = []
        for row in self._rows(path):
            try:
                ts_event = int(row["timestamp"])
                if end_ts is not None and ts_event > end_ts:
                    continue
                selected.append(
                    RawDerivativePoint(
                        kind=DerivativeKind.OPEN_INTEREST,
                        symbol=symbol,
                        timeframe=timeframe,
                        ts_event=ts_event,
                        value=Decimal(row["openInterest"]),
                        ts_available=ts_event + interval,
                        ts_ingested=ts_event + interval,
                        ts_committed=ts_event + interval,
                        precision=PrecisionTier.SECOND_VERIFIED,
                    )
                )
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise FeedUnavailableError(f"离线持仓量数据格式错误：{path}") from exc
            if len(selected) >= limit:
                break
        selected.reverse()
        return selected

    def _rows(self, path: Path) -> list:
        if not path.is_file():
            raise FeedUnavailableError(f"离线样本缺少衍生品文件（{path}）")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FeedUnavailableError(f"离线样本无法读取：{path}") from exc
        if payload.get("retCode") != 0:
            raise FeedUnavailableError(
                f"离线样本返回错误 {payload.get('retCode')}: {payload.get('retMsg')}"
            )
        return list(payload.get("result", {}).get("list", []))
