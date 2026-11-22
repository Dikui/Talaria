"""在 Notebook 或脚本中按时点读取 K 线。"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from itertools import chain
from math import ceil, floor
from pathlib import Path
from typing import TYPE_CHECKING, overload

import httpx

from talaria.foundation.bars import MAX_RANGE_BARS, BarsQuery, BarsResponse, RangeBar
from talaria.foundation.errors import InstrumentNotFoundError, InvalidRequestError
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES, last_closed_bar_start, timeframe_ms
from talaria.packs import DEFAULT_PACK, get_market_pack
from talaria.perception.cold_store import ColdReader
from talaria.perception.raw_store import latest_visible_bars
from talaria.sdk._subscription import StrategyBarEvent, SubscriptionRejectedError, subscribe
from talaria.service_endpoint import DATA_DIR_HEADER, NOW_MS_HEADER, data_dir_id, read_service_endpoint

if TYPE_CHECKING:
    from pandas import DataFrame

__all__ = ["BarsResult", "StrategyBarEvent", "SubscriptionRejectedError", "bars", "subscribe"]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
# datetime 校验 ISO 格式并解析到微秒。保留时钟和时区偏移中被截掉的小数位。
_ISO_SUBMICROSECONDS = re.compile(
    r"[0-9]{4}(?:-[0-9]{2}-[0-9]{2}|[0-9]{4}|-W[0-9]{2}(?:-[0-9])?|W[0-9]{2}[0-9]?)."
    r"[0-9]{2}(?::?[0-9]{2}){0,2}(?:[.,][0-9]{1,6}([0-9]+)?)?"
    r"(?:Z|([+-])[0-9]{2}(?::?[0-9]{2}){0,2}(?:[.,][0-9]{1,6}([0-9]+)?)?)?",
    re.DOTALL,
)


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


def _time_ms(value: int | str) -> int | Fraction:
    if isinstance(value, int):
        return value
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    micros: int | Fraction = (timestamp - _EPOCH) // _MICROSECOND
    parts = _ISO_SUBMICROSECONDS.fullmatch(value)
    if parts is not None:
        submicro, offset_sign, offset_submicro = parts.groups()
        if submicro:
            micros += Fraction(Decimal(f"0.{submicro}"))
        if offset_submicro:
            micros -= Fraction(Decimal(f"{offset_sign}0.{offset_submicro}"))
    return Fraction(micros, 1_000)


def _fetch_tail(
    base_url: str, query: BarsQuery, directory_id: str,
) -> tuple[int, set[int], list[RangeBar]] | None:
    interval = timeframe_ms(query.timeframe)
    span = MAX_RANGE_BARS * interval
    tail: list[RangeBar] = []
    generations: set[int] = set()
    params = query.model_dump(exclude_none=True, exclude={"start", "end"})
    timeout = httpx.Timeout(35, connect=1)
    try:
        # 服务等待历史回填最多 30 秒。连接失效时尽快使用本地数据。
        with httpx.Client(base_url=base_url, trust_env=False, timeout=timeout) as client:
            health = client.get("/api/health", timeout=1)
            if not health.is_success or health.headers.get(DATA_DIR_HEADER) != directory_id:
                return None
            end = query.end
            if end is None:
                try:
                    now = int(health.headers.get(NOW_MS_HEADER, ""))
                except ValueError:
                    return None
                end = last_closed_bar_start(now, interval)
            for start in range(query.start, end + 1, span):
                chunk = dict(params, start=start, end=min(end, start + span - 1))
                response = client.get("/api/bars", params=chunk)
                response.raise_for_status()
                if response.headers.get(DATA_DIR_HEADER) != directory_id:
                    return None
                payload = BarsResponse.model_validate_json(response.content)
                if (payload.instrument_id, payload.timeframe) != (query.instrument, query.timeframe):
                    raise ValueError("服务返回的标的或周期与请求不一致")
                generations.add(payload.generation)
                tail.extend(payload.bars)
    except httpx.TransportError:
        return None
    return end, generations, tail


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

    整数采用 UTC 毫秒，无时区的 ISO 日期时间采用 UTC。省略 ``end`` 时取最后一根已收盘柱。
    数据目录优先使用
    ``data_dir``，其次为 ``TALARIA_DATA_DIR``，默认 ``./.talaria``。
    ``offline=True`` 时直接读取本地清单。
    """
    exact_start = _time_ms(start)
    exact_end = None if end is None else _time_ms(end)
    if exact_end is not None and exact_end < exact_start:
        raise ValueError("end 不得早于 start")
    query = BarsQuery(
        # 先校验输入的毫秒范围。随后向上取整可能让区间变空。
        instrument=instrument, timeframe=timeframe, start=floor(exact_start),
        end=None if exact_end is None else floor(exact_end),
        as_of=None if as_of is None else floor(_time_ms(as_of)),
        tail_only=True,
    )
    spec = get_market_pack(DEFAULT_PACK).instrument(query.instrument)
    if spec is None:
        raise InstrumentNotFoundError(f"未知标的：{query.instrument}")
    if query.timeframe not in SUPPORTED_TIMEFRAMES:
        raise InvalidRequestError(f"不支持的周期：{query.timeframe}")
    start_ts = ceil(exact_start)
    if query.end is not None and query.end < start_ts:
        return BarsResult((), tail_completed=False)
    query = query.model_copy(update={"start": start_ts})

    root = Path(data_dir or os.environ.get("TALARIA_DATA_DIR") or ".talaria").expanduser().resolve()
    endpoint = None if offline else read_service_endpoint(root)
    reader = ColdReader(root)
    if endpoint is not None:
        directory_id = data_dir_id(root)
        # 回填或实时写入可能让各分段和直读取得不同的清单。整次重试后仍不同则只读本地。
        for _ in range(2):
            response = _fetch_tail(endpoint, query, directory_id)
            if response is None:
                break
            end_ts, generations, tail = response
            if end_ts < query.start:
                return BarsResult((), tail_completed=True)
            if len(generations) != 1:
                continue
            generation, cold = reader.read_manifest_revisions(
                spec, query.timeframe, start_ts=query.start, end_ts=end_ts,
            )
            if generations == {generation}:
                archived = (
                    RangeBar.model_validate(bar, from_attributes=True)
                    for bar in latest_visible_bars(cold, as_of=query.as_of)
                )
                visible = latest_visible_bars(
                    chain(archived, (bar for bar in tail if bar.confirmed)), as_of=query.as_of,
                )
                return BarsResult(tuple(visible), tail_completed=True)

    end_ts = query.end
    if end_ts is None:
        end_ts = last_closed_bar_start(time.time_ns() // 1_000_000, timeframe_ms(query.timeframe))
    cold = reader.read_bars(spec, query.timeframe, start_ts=query.start, end_ts=end_ts, as_of=query.as_of)
    return BarsResult(tuple(RangeBar.model_validate(bar, from_attributes=True) for bar in cold), False)
