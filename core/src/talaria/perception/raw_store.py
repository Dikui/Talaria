"""原始标准化 K 线落盘：JSONL 追加、fsync、按 timestamp 去重。

路径固定为 ``<data_dir>/data/raw/bybit_linear/<SYMBOL>/<timeframe>.jsonl``。
去重语义是「last-ingested wins」：后追加的同 timestamp 记录覆盖先前的。
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from talaria.foundation.connector import RawBar
from talaria.foundation.types import PrecisionTier

RAW_SUBDIR = Path("data") / "raw" / "bybit_linear"


def bar_record(bar: RawBar) -> dict[str, object]:
    """把原始柱转成稳定字段顺序的 JSON 记录（冷库归档也复用这一形状）。"""
    return {
        "symbol": bar.symbol,
        "timeframe": bar.timeframe,
        "ts_event": bar.ts_event,
        "open": format(bar.open, "f"),
        "high": format(bar.high, "f"),
        "low": format(bar.low, "f"),
        "close": format(bar.close, "f"),
        "volume": format(bar.volume, "f"),
        "turnover": format(bar.turnover, "f"),
        "ts_available": bar.ts_available,
        "ts_ingested": bar.ts_ingested,
        "ts_committed": bar.ts_committed,
        "precision": bar.precision.value,
    }


def bar_from_record(record: dict[str, object]) -> RawBar:
    return RawBar(
        symbol=str(record["symbol"]),
        timeframe=str(record["timeframe"]),
        ts_event=int(record["ts_event"]),  # type: ignore[arg-type]
        open=Decimal(str(record["open"])),
        high=Decimal(str(record["high"])),
        low=Decimal(str(record["low"])),
        close=Decimal(str(record["close"])),
        volume=Decimal(str(record["volume"])),
        turnover=Decimal(str(record["turnover"])),
        ts_available=int(record["ts_available"]),  # type: ignore[arg-type]
        ts_ingested=int(record["ts_ingested"]),  # type: ignore[arg-type]
        ts_committed=int(record["ts_committed"]),  # type: ignore[arg-type]
        precision=PrecisionTier(str(record["precision"])),
    )


class RawStore:
    """按标的与周期追加原始柱，读取时去重并升序返回。"""

    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)

    def path_for(self, symbol: str, timeframe: str) -> Path:
        return self._data_dir / RAW_SUBDIR / symbol / f"{timeframe}.jsonl"

    def append(self, symbol: str, timeframe: str, bars: Iterable[RawBar]) -> int:
        materialized = list(bars)
        if not materialized:
            return 0
        path = self.path_for(symbol, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for bar in materialized:
                handle.write(
                    json.dumps(bar_record(bar), ensure_ascii=False, sort_keys=True) + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())
        return len(materialized)

    def read(self, symbol: str, timeframe: str) -> list[RawBar]:
        path = self.path_for(symbol, timeframe)
        if not path.is_file():
            return []
        by_timestamp: dict[int, RawBar] = {}
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                bar = bar_from_record(record)
                by_timestamp[bar.ts_event] = bar
        return [by_timestamp[key] for key in sorted(by_timestamp)]
