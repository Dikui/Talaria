"""按标的、周期和时点查询冷库及热缓冲中的 K 线。"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from talaria.foundation.bars import MAX_RANGE_BARS, BarsQuery, BarsResponse, RangeBar
from talaria.foundation.connector import RawBar
from talaria.foundation.errors import (
    BarsBackfillTimeoutError,
    BarsRangeTooLargeError,
    FeedUnavailableError,
    InstrumentNotFoundError,
    InvalidRequestError,
)
from talaria.foundation.snapshot import ErrorResponse
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES, last_closed_bar_start, timeframe_ms
from talaria.perception.bar_range import range_visible_bars, read_bar_range, revision_window
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
        503: {"model": ErrorResponse, "description": "范围重取不完整，可以重试"},
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
    if query.resync_from is not None:
        history_start = max(start, (query.resync_from + interval - 1) // interval * interval)
    if (end - history_start) // interval + 1 > MAX_RANGE_BARS:
        raise BarsRangeTooLargeError(RANGE_LIMIT_MESSAGE)
    read_range = partial(
        read_bar_range, ColdReader(settings.data_dir), runtime.hot_buffer,
        instrument, query.timeframe, query.start, end,
    )
    generation, cold, hot = await asyncio.to_thread(read_range)
    publisher = runtime.cold_publisher
    has_rest = publisher is not None and (settings.replay_events is None or settings.fixture_dir is not None)
    cutoff = now if query.as_of is None else min(now, query.as_of)
    history_end = min(end, cutoff - interval)

    def coverage() -> dict[int, RawBar | None]:
        slots = dict.fromkeys(range(history_start, history_end + 1, interval))
        slots.update((bar.ts_event, None) for bar in hot if bar.ts_event <= history_end)
        visible = range_visible_bars(cold, hot, as_of=cutoff, include_open=False)
        slots.update((bar.ts_event, bar) for bar in visible if bar.ts_event <= history_end)
        return slots

    history = None
    if query.resync_from is not None:
        expected = await asyncio.to_thread(coverage)
        if len(expected) > MAX_RANGE_BARS:
            raise BarsRangeTooLargeError(RANGE_LIMIT_MESSAGE)
        if expected and has_rest:
            history = publisher.refresh_history(instrument, query.timeframe, expected)
    elif has_rest and history_start <= history_end:
        def missing_history() -> frozenset[int] | None:
            hot_slots = frozenset(bar.ts_event for bar in hot if bar.confirmed and not bar.synthetic)
            covered = hot_slots.union(bar.ts_event for bar in cold if bar.confirmed)
            if any(ts not in covered for ts in range(history_start, history_end + 1, interval)):
                return hot_slots
            return None
        hot_slots = await asyncio.to_thread(missing_history)
        if hot_slots is not None:
            history = publisher.request_history(
                instrument, query.timeframe, start_ts=history_start, end_ts=history_end,
                hot_timestamps=hot_slots,
            )
    if history is not None:
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(history)), timeout=HISTORY_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise BarsBackfillTimeoutError("等待 K 线历史回填超时，请重试") from exc
        generation, cold, hot = await asyncio.to_thread(read_range)

    def select() -> BarsResponse:
        if query.resync_from is not None:
            # REST 新发现的修订在服务实际收到时才可见。
            as_of = settings.now_ms() if query.as_of is None else cutoff
            visible, _ = revision_window(cold, hot, since=0, as_of=as_of)
            covered = {bar.ts_event for bar in visible}
            if not covered.issuperset(expected):
                raise FeedUnavailableError("范围重取缺少已确认柱，请重试")
        elif query.available_since is not None:
            visible, _ = revision_window(cold, hot, since=query.available_since, as_of=cutoff)
        else:
            as_of = now if query.as_of is None else query.as_of
            visible = range_visible_bars(cold, hot, as_of=as_of, include_open=query.include_open)
        if query.tail_only:
            archived = set(cold)
            visible = [bar for bar in visible if bar not in archived]
        if len(visible) > MAX_RANGE_BARS:
            raise BarsRangeTooLargeError(RANGE_LIMIT_MESSAGE)
        return BarsResponse(
            instrument_id=instrument.instrument_id, timeframe=query.timeframe, generation=generation,
            bars=[RangeBar.model_validate(bar, from_attributes=True) for bar in visible],
        )

    response.headers[DATA_DIR_HEADER] = data_dir_id(settings.data_dir)
    return await asyncio.to_thread(select)
