"""按标的、周期和时点查询冷库及热缓冲中的 K 线。"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from talaria.foundation.bars import MAX_RANGE_BARS, BarsQuery, BarsResponse, RangeBar
from talaria.foundation.connector import RawBar
from talaria.foundation.errors import (
    BarsBackfillTimeoutError,
    BarsRangeTooLargeError,
    InstrumentNotFoundError,
    InvalidRequestError,
)
from talaria.foundation.snapshot import ErrorResponse
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES, last_closed_bar_start, timeframe_ms
from talaria.perception.bar_range import range_visible_bars, range_visible_revisions
from talaria.perception.cold_store import ColdReader
from talaria.service_endpoint import DATA_DIR_HEADER, data_dir_id
from talaria.workbench_api.routes import get_runtime

router = APIRouter()
HISTORY_TIMEOUT_SECONDS = 30
RANGE_LIMIT_MESSAGE = f"一次最多查询 {MAX_RANGE_BARS:,} 根 K 线，请分段查询"


@router.get(
    "/api/bars", response_model=BarsResponse,
    responses={
        200: {"headers": {DATA_DIR_HEADER: {"description": "数据目录标识", "schema": {"type": "string"}}}},
        400: {"model": ErrorResponse, "description": "K 线范围超过单次请求上限"},
        504: {"model": ErrorResponse, "description": "等待历史回填超时，可以重试"},
    },
)
async def get_range_bars(query: Annotated[BarsQuery, Query()], request: Request, response: Response) -> BarsResponse:
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

    now = settings.now_ms()
    interval = timeframe_ms(query.timeframe)
    start = (query.start + interval - 1) // interval * interval
    end = last_closed_bar_start(now, interval) if query.end is None else query.end
    # 迟到的修订可能在任意旧柱上。只在重同步窗口附近及之后补缺。
    history_start = start
    if query.available_since is not None:
        history_start = max(start, last_closed_bar_start(query.available_since, interval))
    if (end - history_start) // interval + 1 > MAX_RANGE_BARS:
        raise BarsRangeTooLargeError(RANGE_LIMIT_MESSAGE)
    reader = ColdReader(settings.data_dir)

    def read_hot() -> list[RawBar]:
        return [] if runtime.hot_buffer is None else runtime.hot_buffer.revisions(
            instrument_id=instrument.instrument_id, timeframe=query.timeframe,
            start_ts=query.start, end_ts=end,
        )

    def read_range() -> tuple[int, list[RawBar], list[RawBar]]:
        # 保留读取期间从热端淘汰的版本，也纳入与新落盘修订匹配的实时发布。
        before = read_hot()
        generation, cold = reader.read_manifest_revisions(
            instrument, query.timeframe, start_ts=query.start, end_ts=end,
        )
        after = read_hot()
        if before == after:
            return generation, cold, before
        hot = {(bar.ts_event, bar.revision): bar for revisions in (before, after) for bar in revisions}
        return generation, cold, [hot[key] for key in sorted(hot)]

    generation, cold, hot = await asyncio.to_thread(read_range)
    publisher = runtime.cold_publisher
    if publisher is not None and (settings.replay_events is None or settings.fixture_dir is not None):
        cutoff = now if query.as_of is None else min(now, query.as_of)
        history_end = min(end, cutoff - interval)
        if history_start <= history_end:
            hot_timestamps = frozenset(
                bar.ts_event for bar in hot
                if bar.confirmed and not bar.synthetic and history_start <= bar.ts_event <= history_end
            )
            covered = hot_timestamps.union(
                bar.ts_event for bar in cold if history_start <= bar.ts_event <= history_end
            )
            if sum(ts % interval == 0 for ts in covered) < (history_end - history_start) // interval + 1:
                history = publisher.request_history(
                    instrument, query.timeframe, start_ts=history_start, end_ts=history_end,
                    hot_timestamps=hot_timestamps,
                )
                if history is not None:
                    try:
                        # 同一范围的多个请求可以共用一个后台回填。
                        await asyncio.wait_for(
                            asyncio.shield(asyncio.wrap_future(history)), timeout=HISTORY_TIMEOUT_SECONDS,
                        )
                    except TimeoutError as exc:
                        raise BarsBackfillTimeoutError("等待 K 线历史回填超时，请重试") from exc
                    generation, cold, hot = await asyncio.to_thread(read_range)

    if query.available_since is None:
        visible = range_visible_bars(cold, hot, as_of=query.as_of, include_open=query.include_open)
    else:
        visible = await asyncio.to_thread(
            range_visible_revisions, cold, hot, since=query.available_since, as_of=query.as_of,
        )
    if query.tail_only:
        archived = set(cold)
        visible = [bar for bar in visible if bar not in archived]
    if len(visible) > MAX_RANGE_BARS:
        raise BarsRangeTooLargeError(RANGE_LIMIT_MESSAGE)
    response.headers[DATA_DIR_HEADER] = data_dir_id(settings.data_dir)
    return BarsResponse(
        instrument_id=instrument.instrument_id,
        timeframe=query.timeframe,
        generation=generation,
        bars=[RangeBar.model_validate(bar, from_attributes=True) for bar in visible],
    )
