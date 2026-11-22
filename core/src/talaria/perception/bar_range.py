"""把热缓冲的进程内修订接到冷库的已发布修订上。"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import replace
from decimal import Decimal
from itertools import chain, groupby

from talaria.foundation.connector import RawBar
from talaria.foundation.types import PrecisionTier
from talaria.perception.raw_store import latest_visible_bars


def _values(bar: RawBar) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal, PrecisionTier]:
    return (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.turnover, bar.precision)


def next_confirmed_revision(bar: RawBar, previous: RawBar | None) -> RawBar:
    """写入与查询共用修订规则，沿用相同内容的已发布版本。"""
    if previous is None:
        return bar
    if _values(bar) == _values(previous):
        return previous
    return replace(
        bar,
        revision=max(bar.revision, previous.revision + 1),
        ts_available=max(bar.ts_available, bar.ts_ingested, previous.ts_available),
    )


def _hot_confirmed(cold: Sequence[RawBar], hot: Sequence[RawBar]) -> Iterator[RawBar]:
    # 同一次实时发布在冷热端使用同一个接收时刻，冷库编号跨进程延续。
    archived = sorted(cold, key=lambda bar: (bar.revision, bar.ts_available, bar.ts_ingested))
    matches: deque[tuple[int, int]] = deque()
    cursor = 0
    for bar in hot:
        publication = (bar.ts_ingested, _values(bar))
        for index in range(cursor, len(archived)):
            stored = archived[index]
            if (stored.ts_ingested, _values(stored)) == publication:
                matches.append((bar.revision, index))
                cursor = index + 1
                break

    cursor = 0
    previous = None
    for bar in hot:
        next_revision, next_index = matches[0] if matches else (-1, len(archived))
        if bar.revision == next_revision:
            previous = archived[next_index]
            cursor = next_index + 1
            matches.popleft()
            continue
        while cursor < next_index and archived[cursor].ts_ingested <= bar.ts_ingested:
            previous = archived[cursor]
            cursor += 1
        if bar.history_only:
            if previous is None and not archived:
                yield bar
            continue
        version = next_confirmed_revision(bar, previous)
        if version is not previous:
            yield version
            previous = version


def range_visible_bars(
    cold: Sequence[RawBar], hot: Sequence[RawBar], *, as_of: int | None, include_open: bool,
) -> list[RawBar]:
    """按同一时点规则选版，热端输入按事件时间与修订升序。"""
    confirmed: list[RawBar] = []
    previews: list[RawBar] = []
    hot_by_event = {ts: list(group) for ts, group in groupby(hot, key=lambda bar: bar.ts_event)}
    cold_by_event: dict[int, list[RawBar]] = {}
    for bar in cold:
        if bar.ts_event in hot_by_event:
            cold_by_event.setdefault(bar.ts_event, []).append(bar)
    for ts, versions in hot_by_event.items():
        confirmed.extend(_hot_confirmed(cold_by_event.get(ts, []), [b for b in versions if b.confirmed]))
        if include_open:
            previews.extend(
                replace(bar, ts_available=max(bar.ts_event, bar.ts_ingested))
                for bar in versions if not bar.confirmed
            )
    closed = latest_visible_bars(chain(cold, confirmed), as_of=as_of)
    if not previews:
        return closed
    closed_events = {bar.ts_event for bar in closed}
    open_bars = latest_visible_bars(
        (bar for bar in previews if bar.ts_event not in closed_events), as_of=as_of,
    )
    return sorted((*closed, *open_bars), key=lambda bar: bar.ts_event)
