"""在 Notebook 或脚本中按时点读取 K 线。"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from itertools import chain
from math import ceil, floor
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, overload

import httpx

from talaria.foundation.bars import MAX_RANGE_BARS, BarsQuery, BarsResponse, RangeBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.errors import InvalidRequestError, TalariaError
from talaria.foundation.indicators import IndicatorParams
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.operators import IndicatorName
from talaria.foundation.ticks import Quote, TickKind, Trade
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES, last_closed_bar_start, timeframe_ms
from talaria.perception.cold_store import ColdManifest, ColdReader
from talaria.perception.indicator_defaults import read_indicator_defaults
from talaria.perception.raw_store import latest_visible_bars
from talaria.sdk._resolve import default_data_dir, list_instruments, resolve_instrument_spec
from talaria.sdk._arrow import (
    ARROW_STREAM_CONTENT_TYPE,
    bars_from_arrow,
    bars_to_arrow,
    derivatives_from_arrow,
    quotes_from_arrow,
    trades_from_arrow,
)
from talaria.sdk._derivatives import DerivativesResult
from talaria.sdk._duckdb import asof_join, cold_parquet_paths, query_duckdb
from talaria.sdk._ticks import QuotesResult, TradesResult
from talaria.sdk._subscription import (
    StrategyBarEvent, StrategyDerivativeEvent, SubscriptionRejectedError, subscribe, subscribe_derivatives,
)
from talaria.service_endpoint import (
    BARS_HOT_REVISION_HEADER,
    BARS_HOT_REVISION_UNSTABLE,
    BARS_MANIFEST_HEADER,
    DATA_DIR_HEADER,
    NOW_MS_HEADER,
    data_dir_id,
    read_service_endpoint,
)

if TYPE_CHECKING:
    import pyarrow as pa
    from pandas import DataFrame

__all__ = [
    "BarsConsistencyError",
    "BarsResult",
    "DerivativesResult",
    "QuotesResult",
    "TradesResult",
    "StrategyBarEvent",
    "StrategyDerivativeEvent",
    "SubscriptionRejectedError",
    "asof_join",
    "bars",
    "cold_parquet_paths",
    "data_dir",
    "derivatives",
    "funding_settlements",
    "indicator_defaults",
    "instruments",
    "query_duckdb",
    "quotes",
    "trades",
    "subscribe",
    "subscribe_derivatives",
]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
# datetime 校验 ISO 格式并解析到微秒。保留时钟和时区偏移中被截掉的小数位。
_ISO_SUBMICROSECONDS = re.compile(
    r"[0-9]{4}(?:-[0-9]{2}-[0-9]{2}|[0-9]{4}|-W[0-9]{2}(?:-[0-9])?|W[0-9]{2}[0-9]?)."
    r"[0-9]{2}(?::?[0-9]{2}){0,2}(?:[.,][0-9]{1,6}([0-9]+)?)?"
    r"(?:Z|([+-])[0-9]{2}(?::?[0-9]{2}){0,2}(?:[.,][0-9]{1,6}([0-9]+)?)?)?",
    re.DOTALL,
)


class BarsConsistencyError(TalariaError):
    """补尾与冷库未能取得一致视图，可以重试查询。"""

    code = "BARS_CONSISTENCY"


@dataclass(frozen=True, slots=True)
class BarsResult(Sequence[RangeBar]):
    """每个事件时间仅含最高可见确认修订，按事件时间升序排列。

    ``tail_completed`` 表示服务已查询完整个范围。
    """

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

    def to_arrow(self) -> "pa.Table":
        """转换为保留精确 decimal128 OHLCV 数值的 PyArrow 表。"""
        return bars_to_arrow(self._bars)

    def query_duckdb(
        self,
        sql: str,
        *,
        table: str = "bars",
        params: Sequence[object] | Mapping[str, object] | None = None,
        as_arrow: bool = False,
    ) -> "DataFrame | pa.Table":
        """在只读内存 DuckDB 上查询当前 K 线结果。"""
        return query_duckdb(sql, tables={table: self.to_arrow()}, params=params, as_arrow=as_arrow)

    def asof_join(
        self,
        other: Any,
        *,
        on: str = "ts_event",
        by: Sequence[str] | str | None = None,
        right_columns: Sequence[str] | None = None,
        right_prefix: str = "right_",
        tolerance_ms: int | None = None,
        as_arrow: bool = False,
    ) -> "DataFrame | pa.Table":
        """基于 DuckDB ``ASOF LEFT JOIN`` 与另一序列按时点对齐。"""
        return asof_join(
            self, other, on=on, by=by, right_columns=right_columns,
            right_prefix=right_prefix, tolerance_ms=tolerance_ms, as_arrow=as_arrow,
        )

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


def _data_root(data_dir_path: str | Path | None) -> Path:
    return default_data_dir(data_dir_path)


def data_dir(path: str | Path | None = None) -> Path:
    """返回 SDK 当前使用的数据目录（显式参数 > ``TALARIA_DATA_DIR`` > ``~/.talaria/data``）。"""
    return default_data_dir(path)


def instruments(
    *, data_dir: str | Path | None = None, offline: bool = False,
) -> tuple[InstrumentSpec, ...]:
    """返回在线服务或本地清单中的可用标的规格列表。"""
    return tuple(list_instruments(data_dir=data_dir, offline=offline, client_cls=httpx.Client))


def indicator_defaults(*, data_dir: str | Path | None = None) -> dict[IndicatorName, IndicatorParams]:
    """读取同一数据目录下与工作台一致的指标默认值。"""
    return read_indicator_defaults(_data_root(data_dir))


def trades(
    instrument: str, start: int | str, end: int | str | None = None,
    as_of: int | str | None = None, *, data_dir: str | Path | None = None,
) -> TradesResult:
    """从冷库按闭区间读取逐笔成交，省略 as_of 时按服务或本地时钟截断。"""
    rows = _tick_range(instrument, TickKind.TRADES, start, end, as_of, data_dir)
    return TradesResult(cast(list[Trade], rows))


def quotes(
    instrument: str, start: int | str, end: int | str | None = None,
    as_of: int | str | None = None, *, data_dir: str | Path | None = None,
) -> QuotesResult:
    """从冷库按闭区间读取买一卖一，省略 as_of 时按服务或本地时钟截断。"""
    rows = _tick_range(instrument, TickKind.QUOTES, start, end, as_of, data_dir)
    return QuotesResult(cast(list[Quote], rows))


def _tick_range(
    instrument: str, kind: TickKind, start: int | str, end: int | str | None,
    as_of: int | str | None, data_dir: str | Path | None,
) -> list[Trade | Quote]:
    exact_start = _time_ms(start)
    exact_end = None if end is None else _time_ms(end)
    if exact_end is not None and exact_end < exact_start:
        raise ValueError("end 不得早于 start")
    start_ts = ceil(exact_start)
    end_ts = None if exact_end is None else floor(exact_end)
    root = _data_root(data_dir)
    endpoint = read_service_endpoint(root)
    directory_id = data_dir_id(root)
    spec = resolve_instrument_spec(
        instrument, root=root, endpoint=endpoint, directory_id=directory_id, client_cls=httpx.Client,
    )
    service_now: int | None = None
    if endpoint is not None:
        try:
            with httpx.Client(base_url=endpoint, trust_env=False, timeout=httpx.Timeout(35, connect=1)) as client:
                service_now = _service_clock(client, directory_id)
                if service_now is not None:
                    cutoff = service_now if as_of is None else floor(_time_ms(as_of))
                    effective_end = cutoff if end_ts is None else min(cutoff, end_ts)
                    if effective_end < start_ts:
                        return []
                    response = client.get(
                        "/api/ticks.arrow",
                        params={
                            "instrument": instrument,
                            "kind": kind.value,
                            "start": start_ts,
                            "end": effective_end,
                            "as_of": cutoff,
                        },
                    )
                    if response.status_code != 404:
                        response.raise_for_status()
                        if (
                            response.headers.get(DATA_DIR_HEADER) == directory_id
                            and response.headers.get("content-type", "").startswith(ARROW_STREAM_CONTENT_TYPE)
                        ):
                            return (
                                trades_from_arrow(response.content, instrument)
                                if kind is TickKind.TRADES
                                else quotes_from_arrow(response.content, instrument)
                            )
                    return ColdReader(root).read_ticks(
                        spec, kind, start_ts=start_ts, end_ts=effective_end, as_of=cutoff,
                    )
        except httpx.TransportError:
            pass
    cutoff = (service_now if service_now is not None else time.time_ns() // 1_000_000) if as_of is None else floor(_time_ms(as_of))
    effective_end = cutoff if end_ts is None else min(cutoff, end_ts)
    if effective_end < start_ts:
        return []
    return ColdReader(root).read_ticks(
        spec, kind, start_ts=start_ts, end_ts=effective_end, as_of=cutoff,
    )


def derivatives(
    instrument: str, kind: DerivativeKind | str, start: int | str,
    end: int | str | None = None, as_of: int | str | None = None, *,
    data_dir: str | Path | None = None,
) -> DerivativesResult:
    """按闭区间读取衍生品。服务在线时先等待历史回填，再读取本地清单。"""
    return _derivative_range(instrument, DerivativeKind(kind), start, end, as_of, data_dir)


def _derivative_range(
    instrument: str, kind: DerivativeKind, start: int | str, end: int | str | None,
    as_of: int | str | None, data_dir: str | Path | None, *, historical_only: bool = False,
) -> DerivativesResult:
    exact_start = _time_ms(start)
    exact_end = None if end is None else _time_ms(end)
    if exact_end is not None and exact_end < exact_start:
        raise ValueError("end 不得早于 start")
    root = _data_root(data_dir)
    endpoint = read_service_endpoint(root)
    directory_id = data_dir_id(root)
    spec = resolve_instrument_spec(
        instrument, root=root, endpoint=endpoint, directory_id=directory_id, client_cls=httpx.Client,
    )
    start_ts = ceil(exact_start)
    end_ts = None if exact_end is None else floor(exact_end)
    cutoff = None if as_of is None else floor(_time_ms(as_of))
    if end_ts is not None and end_ts < start_ts:
        return DerivativesResult(())
    now: int | None = None
    if endpoint is not None:
        try:
            with httpx.Client(base_url=endpoint, trust_env=False, timeout=httpx.Timeout(35, connect=1)) as client:
                now = _service_clock(client, directory_id)
                if now is not None:
                    effective_end = now if end_ts is None else end_ts
                    effective_as_of = now if cutoff is None else cutoff
                    if max(0, start_ts) <= min(now, effective_end) and (cutoff is None or start_ts <= cutoff):
                        backfill_params: dict[str, object] = {
                            "instrument": instrument,
                            "kind": kind.value,
                            "start": max(0, start_ts),
                            "end": effective_end,
                        }
                        if cutoff is not None:
                            backfill_params["as_of"] = cutoff
                        backfill = client.get("/api/derivatives/backfill", params=backfill_params)
                        if backfill.status_code != 404:
                            backfill.raise_for_status()
                            if backfill.headers.get(DATA_DIR_HEADER) != directory_id:
                                now = None
                    if now is not None:
                        arrow_params: dict[str, object] = {
                            "instrument": instrument,
                            "kind": kind.value,
                            "start": start_ts,
                            "end": effective_end,
                            "as_of": effective_as_of,
                            "historical_only": historical_only,
                        }
                        arrow_resp = client.get("/api/derivatives.arrow", params=arrow_params)
                        if arrow_resp.status_code != 404:
                            arrow_resp.raise_for_status()
                            if (
                                arrow_resp.headers.get(DATA_DIR_HEADER) == directory_id
                                and arrow_resp.headers.get("content-type", "").startswith(ARROW_STREAM_CONTENT_TYPE)
                            ):
                                return DerivativesResult(derivatives_from_arrow(
                                    arrow_resp.content,
                                    expected_instrument=instrument,
                                    expected_kind=kind,
                                ))
        except httpx.TransportError:
            pass
    if now is None:
        now = time.time_ns() // 1_000_000
    return DerivativesResult(ColdReader(root).read_derivatives(
        spec, kind, start_ts=start_ts, end_ts=now if end_ts is None else end_ts,
        as_of=now if cutoff is None else cutoff, historical_only=historical_only,
    ))


def _archived_derivative_revisions(
    instrument: str, kind: DerivativeKind, *, end: int, data_dir: str | Path | None,
) -> list[DerivativePoint]:
    root = _data_root(data_dir)
    spec = resolve_instrument_spec(instrument, root=root, offline=True)
    return ColdReader(root).read_derivative_revisions(spec, kind, start_ts=0, end_ts=end)


def _service_clock(client: httpx.Client, directory_id: str) -> int | None:
    health = client.get("/api/health", timeout=1)
    if health.is_success and health.headers.get(DATA_DIR_HEADER) == directory_id:
        try:
            return int(health.headers.get(NOW_MS_HEADER, ""))
        except ValueError:
            pass
    return None


def funding_settlements(
    instrument: str, start: int | str, end: int | str | None = None,
    as_of: int | str | None = None, *, data_dir: str | Path | None = None,
) -> DerivativesResult:
    """返回可见的资金费率结算事件，供成交插件按结算时间生成账本事件。"""
    return DerivativesResult(
        point for point in _derivative_range(
            instrument, DerivativeKind.FUNDING_RATE, start, end, as_of, data_dir, historical_only=True,
        )
        if point.is_funding_settlement
    )


@dataclass(frozen=True, slots=True)
class _TailSegment:
    start: int
    end: int
    generation: int
    fingerprint: str | None
    hot_revision: str | None

    def matches(self, manifest: ColdManifest) -> bool:
        if self.fingerprint is None:  # 兼容未返回范围指纹的旧服务。
            return self.generation == manifest.version
        return self.fingerprint in (
            manifest.range_fingerprint(self.start, self.end, compact=True),
            manifest.range_fingerprint(self.start, self.end),
        )


def _fetch_tail(
    base_url: str,
    query: BarsQuery,
    directory_id: str,
    *,
    expected_symbol: str | None = None,
) -> tuple[int, int, list[_TailSegment], list[RangeBar]] | None:
    resolved_symbol = expected_symbol or query.instrument
    interval = timeframe_ms(query.timeframe)
    span = MAX_RANGE_BARS * interval
    tail: list[RangeBar] = []
    segments: list[_TailSegment] = []
    params = query.model_dump(exclude_none=True, exclude={"start", "end", "as_of"})
    timeout = httpx.Timeout(35, connect=1)
    try:
        # 服务等待历史回填最多 30 秒。连接失效时尽快使用本地数据。
        with httpx.Client(base_url=base_url, trust_env=False, timeout=timeout) as client:
            health = client.get("/api/health", timeout=1)
            if not health.is_success or health.headers.get(DATA_DIR_HEADER) != directory_id:
                return None
            end, cutoff = query.end, query.as_of
            if end is None or cutoff is None:
                try:
                    now = int(health.headers[NOW_MS_HEADER])
                except (KeyError, ValueError):
                    return None
                end = last_closed_bar_start(now, interval) if end is None else end
                cutoff = now if cutoff is None else cutoff
            if end < query.start:
                return end, cutoff, segments, tail

            arrow_resp = client.get(
                "/api/bars.arrow",
                params=dict(params, start=query.start, end=end, as_of=cutoff),
            )
            if arrow_resp.status_code != 404:
                arrow_resp.raise_for_status()
                if arrow_resp.headers.get(DATA_DIR_HEADER) != directory_id:
                    return None
                content_type = arrow_resp.headers.get("content-type", "")
                if content_type.startswith(ARROW_STREAM_CONTENT_TYPE):
                    tail.extend(bars_from_arrow(
                        arrow_resp.content,
                        expected_symbol=resolved_symbol,
                        expected_timeframe=query.timeframe,
                        confirmed=not query.include_open,
                    ))
                    segments.append(_TailSegment(
                        query.start,
                        end,
                        0,
                        arrow_resp.headers.get(BARS_MANIFEST_HEADER),
                        arrow_resp.headers.get(BARS_HOT_REVISION_HEADER),
                    ))
                    return end, cutoff, segments, tail
                payload = BarsResponse.model_validate_json(arrow_resp.content)
                if (payload.instrument_id, payload.timeframe) != (query.instrument, query.timeframe):
                    raise ValueError("服务返回的标的或周期与请求不一致")
                segments.append(_TailSegment(
                    query.start,
                    end,
                    payload.generation,
                    arrow_resp.headers.get(BARS_MANIFEST_HEADER),
                    arrow_resp.headers.get(BARS_HOT_REVISION_HEADER),
                ))
                tail.extend(payload.bars)
                return end, cutoff, segments, tail

            for start in range(query.start, end + 1, span):
                segment_end = min(end, start + span - 1)
                chunk = dict(params, start=start, end=segment_end, as_of=cutoff)
                response = client.get("/api/bars", params=chunk)
                response.raise_for_status()
                if response.headers.get(DATA_DIR_HEADER) != directory_id:
                    return None
                payload = BarsResponse.model_validate_json(response.content)
                if (payload.instrument_id, payload.timeframe) != (query.instrument, query.timeframe):
                    raise ValueError("服务返回的标的或周期与请求不一致")
                segments.append(_TailSegment(
                    start, segment_end, payload.generation, response.headers.get(BARS_MANIFEST_HEADER),
                    response.headers.get(BARS_HOT_REVISION_HEADER),
                ))
                tail.extend(payload.bars)
    except httpx.TransportError:
        return None
    return end, cutoff, segments, tail


def bars(
    instrument: str,
    timeframe: str,
    start: int | str,
    end: int | str | None = None,
    as_of: int | str | None = None,
    *,
    data_dir: str | Path | None = None,
    offline: bool = False,
    _cold_reader: ColdReader | None = None,
) -> BarsResult:
    """读取指定范围内已确认的 K 线，两个事件时间边界均包含。

    整数采用 UTC 毫秒，无时区的 ISO 日期时间采用 UTC。省略 ``end`` 时取最后一根已收盘柱。
    在线查询省略 ``as_of`` 时，直读和补尾共用服务时钟给出的截点。
    数据目录优先使用
    ``data_dir``，其次为 ``TALARIA_DATA_DIR``，默认 ``~/.talaria/data``。
    ``offline=True`` 时直接读取本地清单。
    在线补尾与直读重试后仍不一致时抛出 ``BarsConsistencyError``。
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
    if query.timeframe not in SUPPORTED_TIMEFRAMES:
        raise InvalidRequestError(f"不支持的周期：{query.timeframe}")
    start_ts = ceil(exact_start)

    endpoint = None
    root = _data_root(data_dir)
    if _cold_reader is None:
        reader = ColdReader(root)
        if not offline:
            endpoint = read_service_endpoint(root)
    else:
        reader = _cold_reader
    directory_id = data_dir_id(root) if endpoint is not None else None
    spec = resolve_instrument_spec(
        query.instrument,
        root=root,
        endpoint=endpoint,
        directory_id=directory_id,
        offline=offline,
        reader=reader,
        client_cls=httpx.Client,
    )
    if query.end is not None and query.end < start_ts:
        return BarsResult((), tail_completed=False)
    query = query.model_copy(update={"start": start_ts})
    if endpoint is not None:
        assert directory_id is not None
        # 各分段必须来自同一热端版本，并与直读时所选的范围文件一致。
        for _ in range(2):
            response = _fetch_tail(endpoint, query, directory_id, expected_symbol=spec.symbol)
            if response is None:
                break
            end_ts, cutoff, segments, tail = response
            if end_ts < query.start:
                return BarsResult((), tail_completed=True)
            hot_revision = segments[0].hot_revision
            if hot_revision == BARS_HOT_REVISION_UNSTABLE or (len(segments) > 1 and not hot_revision):
                continue
            if any(segment.hot_revision != hot_revision for segment in segments):
                continue
            manifest, cold = reader.read_manifest_snapshot(
                spec, query.timeframe, start_ts=query.start, end_ts=end_ts,
            )
            if all(segment.matches(manifest) for segment in segments):
                archived = (
                    RangeBar.model_validate(bar, from_attributes=True)
                    for bar in latest_visible_bars(cold, as_of=cutoff)
                )
                visible = latest_visible_bars(
                    chain(archived, (bar for bar in tail if bar.confirmed)), as_of=cutoff,
                )
                return BarsResult(tuple(visible), tail_completed=True)
        else:
            raise BarsConsistencyError("K 线补尾或冷库清单变化，未能取得一致视图，请重试")

    end_ts = query.end
    if end_ts is None:
        end_ts = last_closed_bar_start(time.time_ns() // 1_000_000, timeframe_ms(query.timeframe))
    cold = reader.read_bars(spec, query.timeframe, start_ts=query.start, end_ts=end_ts, as_of=query.as_of)
    return BarsResult(tuple(RangeBar.model_validate(bar, from_attributes=True) for bar in cold), False)
