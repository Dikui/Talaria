"""在 Notebook 或脚本中按时点读取 K 线。"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, overload

import httpx

from talaria.foundation.bars import MAX_RANGE_BARS, BarsQuery, BarsResponse, RangeBar
from talaria.foundation.errors import InstrumentNotFoundError, InvalidRequestError
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES, timeframe_ms
from talaria.packs import DEFAULT_PACK, get_market_pack
from talaria.perception.cold_store import ColdReader
from talaria.perception.raw_store import latest_visible_bars
from talaria.service_endpoint import read_service_endpoint

if TYPE_CHECKING:
    from pandas import DataFrame

__all__ = ["BarsResult", "bars"]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND = timedelta(milliseconds=1)


@dataclass(frozen=True, slots=True)
class BarsResult(Sequence[RangeBar]):
    """按事件时间升序的 K 线；``tail_completed`` 表示服务已查询完整个范围。"""

    _bars: tuple[RangeBar, ...]
    tail_completed: bool

    @overload
    def __getitem__(self, index: int) -> RangeBar: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[RangeBar, ...]: ...

    def __getitem__(self, index: int | slice) -> RangeBar | tuple[RangeBar, ...]:
        return self._bars[index]

    def __len__(self) -> int:
        return len(self._bars)

    def to_frame(self) -> DataFrame:
        """以 float64 表示 OHLCV 与成交额，时间列保留 UTC 毫秒。"""
        import pandas as pd

        dtypes = {
            "ts_event": "int64",
            "open": "float64",
            "high": "float64",
            "low": "float64",
            "close": "float64",
            "volume": "float64",
            "turnover": "float64",
            "ts_available": "int64",
            "precision": "string",
            "confirmed": "bool",
            "revision": "int64",
        }
        return pd.DataFrame({
            column: pd.Series((getattr(bar, column) for bar in self._bars), dtype=dtype)
            for column, dtype in dtypes.items()
        })


def _time_ms(value: int | str) -> int:
    if isinstance(value, int):
        return value
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return (timestamp - _EPOCH) // _MILLISECOND


def _fetch_tail(
    base_url: str, query: BarsQuery, end: int,
) -> list[RangeBar] | None:
    span = MAX_RANGE_BARS * timeframe_ms(query.timeframe)
    tail: list[RangeBar] = []
    params = query.model_dump(exclude_none=True, exclude={"start", "end"})
    timeout = httpx.Timeout(35, connect=1)
    try:
        # 服务等待历史回填最多 30 秒。连接失效时尽快使用本地数据。
        with httpx.Client(base_url=base_url, trust_env=False, timeout=timeout) as client:
            for start in range(query.start, max(end, query.start) + 1, span):
                chunk = dict(params, start=start)
                if start <= end:
                    chunk["end"] = min(end, start + span - 1)
                response = client.get("/api/bars", params=chunk)
                response.raise_for_status()
                payload = BarsResponse.model_validate_json(response.content)
                if (payload.instrument_id, payload.timeframe) != (query.instrument, query.timeframe):
                    raise ValueError("服务返回的标的或周期与请求不一致")
                tail.extend(payload.bars)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return None
    return tail


def bars(
    instrument: str,
    timeframe: str,
    start: int | str,
    end: int | str | None = None,
    as_of: int | str | None = None,
    *,
    data_dir: str | Path | None = None,
    offline: bool = False,
) -> BarsResult:
    """读取指定范围内已确认的 K 线，两个事件时间边界均包含。

    整数采用 UTC 毫秒，无时区的 ISO 日期时间采用 UTC。数据目录优先使用
    ``data_dir``，其次为 ``TALARIA_DATA_DIR``，默认 ``./.talaria``。
    ``offline=True`` 时直接读取本地清单。
    """
    query = BarsQuery(
        instrument=instrument, timeframe=timeframe, start=_time_ms(start),
        end=None if end is None else _time_ms(end),
        as_of=None if as_of is None else _time_ms(as_of),
        tail_only=True,
    )
    spec = get_market_pack(DEFAULT_PACK).instrument(query.instrument)
    if spec is None:
        raise InstrumentNotFoundError(f"未知标的：{query.instrument}")
    if query.timeframe not in SUPPORTED_TIMEFRAMES:
        raise InvalidRequestError(f"不支持的周期：{query.timeframe}")

    root = Path(data_dir or os.environ.get("TALARIA_DATA_DIR") or ".talaria").expanduser().resolve()
    endpoint = None if offline else read_service_endpoint(root)
    end_ts = query.end if query.end is not None else time.time_ns() // 1_000_000
    tail = None if endpoint is None else _fetch_tail(endpoint, query, end_ts)

    # 服务可能刚刚按范围回填，等它完成后再打开一版冷库清单。
    cold = ColdReader(root).read_bars(
        spec, query.timeframe, start_ts=query.start,
        end_ts=query.end if tail is None else end_ts, as_of=query.as_of,
    )
    archived = (RangeBar.model_validate(bar, from_attributes=True) for bar in cold)
    visible = latest_visible_bars(
        chain(archived, (bar for bar in tail or () if bar.confirmed)), as_of=query.as_of,
    )
    return BarsResult(tuple(visible), tail_completed=tail is not None)
