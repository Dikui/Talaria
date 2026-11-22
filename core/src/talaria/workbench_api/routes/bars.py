"""按标的、周期和时点查询冷库及热缓冲中的 K 线。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from talaria.foundation.bars import BarsQuery, BarsResponse, RangeBar
from talaria.foundation.connector import RawBar
from talaria.foundation.errors import InstrumentNotFoundError, InvalidRequestError
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES, timeframe_ms
from talaria.perception.bar_range import range_visible_bars
from talaria.perception.cold_store import ColdReader
from talaria.workbench_api.routes import get_runtime

router = APIRouter()


@router.get("/api/bars", response_model=BarsResponse)
def get_range_bars(query: Annotated[BarsQuery, Query()], request: Request) -> BarsResponse:
    runtime = get_runtime(request)
    settings = request.app.state.settings
    instrument = next(
        (spec for spec in runtime.workbench.instruments() if spec.instrument_id == query.instrument),
        None,
    )
    if instrument is None:
        raise InstrumentNotFoundError(f"未知标的：{query.instrument}")
    if query.timeframe not in SUPPORTED_TIMEFRAMES:
        raise InvalidRequestError(f"不支持的周期：{query.timeframe}")

    reader = ColdReader(settings.data_dir)

    def read_range() -> tuple[list[RawBar], list[RawBar]]:
        # 先捕获热端，再读清单。已从热端淘汰的落盘版本仍能从冷库读到。
        hot = [] if runtime.hot_buffer is None else runtime.hot_buffer.revisions(
            instrument_id=instrument.instrument_id, timeframe=query.timeframe,
            start_ts=query.start, end_ts=query.end,
        )
        cold = reader.read_revisions(
            instrument, query.timeframe, start_ts=query.start, end_ts=query.end,
        )
        return cold, hot

    cold, hot = read_range()
    publisher = runtime.cold_publisher
    if publisher is not None and (settings.replay_events is None or settings.fixture_dir is not None):
        now = settings.now_ms()
        cutoff = now if query.as_of is None else min(now, query.as_of)
        interval = timeframe_ms(query.timeframe)
        end = cutoff - interval if query.end is None else min(query.end, cutoff - interval)
        start = (query.start + interval - 1) // interval * interval
        if start <= end:
            hot_timestamps = frozenset(bar.ts_event for bar in hot if start <= bar.ts_event <= end)
            covered = hot_timestamps.union(bar.ts_event for bar in cold if start <= bar.ts_event <= end)
            if sum(ts % interval == 0 for ts in covered) < (end - start) // interval + 1:
                history = publisher.request_history(
                    instrument, query.timeframe, start_ts=start, end_ts=end,
                    hot_timestamps=hot_timestamps,
                )
                if history is not None:
                    history.result()
                    cold, hot = read_range()

    visible = range_visible_bars(
        cold, hot, as_of=query.as_of, include_open=query.include_open,
    )
    return BarsResponse(
        instrument_id=instrument.instrument_id,
        timeframe=query.timeframe,
        bars=[RangeBar.model_validate(bar, from_attributes=True) for bar in visible],
    )
