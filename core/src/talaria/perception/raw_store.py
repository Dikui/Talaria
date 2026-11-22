"""原始标准化 K 线落盘：JSONL 追加、fsync、保留历史修订。

路径为 ``<data_dir>/data/raw/<archive_id>/<SYMBOL>/<timeframe>.jsonl``。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable, Iterator
from decimal import Decimal
from pathlib import Path

from talaria.foundation.bars import RangeBar
from talaria.foundation.connector import RawBar
from talaria.foundation.types import PrecisionTier


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
        "revision": bar.revision,
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
        revision=int(record.get("revision", 0)),  # type: ignore[arg-type]
    )


def latest_visible_bars[Bar: RawBar | RangeBar](
    bars: Iterable[Bar], *, as_of: int | None = None
) -> list[Bar]:
    """先按可获取时间截断，再取每个事件的最高修订（ADR-0018）。

    同一修订按可获取和摄取时间取更晚者，完全相同时后出现者胜。
    """

    chosen: dict[int, tuple[tuple[int, int, int], Bar]] = {}
    for bar in bars:
        if as_of is not None and bar.ts_available > as_of:
            continue
        current = chosen.get(bar.ts_event)
        version = (bar.revision, bar.ts_available, bar.ts_ingested if isinstance(bar, RawBar) else 0)
        if current is None or version >= current[0]:
            chosen[bar.ts_event] = (version, bar)
    return [chosen[ts_event][1] for ts_event in sorted(chosen)]


class RawStore:
    """按标的与周期追加原始柱，读取时去重并升序返回。"""

    def __init__(self, data_dir: Path | str, *, raw_archive_id: str) -> None:
        self._raw_dir = Path(data_dir) / "data" / "raw" / raw_archive_id
        self._lock = threading.Lock()

    def path_for(self, symbol: str, timeframe: str) -> Path:
        return self._raw_dir / symbol / f"{timeframe}.jsonl"

    def append(self, symbol: str, timeframe: str, bars: Iterable[RawBar]) -> int:
        materialized = list(bars)
        if not materialized:
            return 0
        path = self.path_for(symbol, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, path.open("a", encoding="utf-8") as handle:
            for bar in materialized:
                handle.write(
                    json.dumps(bar_record(bar), ensure_ascii=False, sort_keys=True) + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())
        return len(materialized)

    def read(
        self, symbol: str, timeframe: str, *, as_of: int | None = None
    ) -> list[RawBar]:
        return latest_visible_bars(self.revisions(symbol, timeframe), as_of=as_of)

    def revisions(self, symbol: str, timeframe: str) -> Iterator[RawBar]:
        """按追加顺序提供原始柱的全部版本。"""

        path = self.path_for(symbol, timeframe)
        if not path.is_file():
            return
        with self._lock, path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield bar_from_record(json.loads(line))
