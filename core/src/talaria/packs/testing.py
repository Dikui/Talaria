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
from talaria.foundation.errors import FeedUnavailableError
from talaria.foundation.timeframes import timeframe_ms
from talaria.foundation.types import PrecisionTier


class FixtureConnector:
    """按 Bybit 相同的「按 end 向左分页」语义读取本地 fixture。"""

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
