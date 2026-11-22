"""把热缓冲的进程内修订接到冷库的已发布修订上。"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from itertools import chain, groupby

from talaria.foundation.connector import RawBar
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.types import PrecisionTier
from talaria.perception.cold_store import ColdManifest, ColdReader
from talaria.perception.hot_buffer import HotSlot, HotStreamBuffer
from talaria.perception.raw_store import latest_visible_bars


def _values(bar: RawBar) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal, PrecisionTier]:
    return (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.turnover, bar.precision)


def unpublished_revisions(cold: Sequence[RawBar], hot: Sequence[RawBar]) -> dict[int, RawBar]:
    """协调同一时间槽的修订，保留落盘编号和写盘失败留下的编号。"""
    return _reconcile_revisions(cold, hot, include_open=False)


def _reconcile_revisions(
    cold: Sequence[RawBar], hot: Sequence[RawBar], *, include_open: bool,
) -> dict[int, RawBar]:
    archived = sorted(cold, key=lambda bar: (bar.revision, bar.ts_available, bar.ts_ingested))
    matches: deque[tuple[int, int]] = deque()
    cursor = 0
    first_confirmation = True
    for bar in hot:
        if not bar.confirmed or bar.synthetic:
            continue
        publication = (bar.ts_ingested, _values(bar))
        for index in range(cursor, len(archived)):
            stored = archived[index]
            if (stored.ts_ingested, _values(stored)) == publication:
                matches.append((bar.revision, index))
                cursor = index + 1
                break
        else:
            # 服务启动后的首次 WS 确认也可能是已归档版本的同值重发。
            # 用当时的冷库版本作锚，避免后来的 REST 修订反过来给旧确认续号。
            if first_confirmation:
                for index in reversed(range(len(archived))):
                    stored = archived[index]
                    if stored.ts_ingested > bar.ts_ingested:
                        continue
                    if _values(stored) == _values(bar):
                        matches.append((bar.revision, index))
                        cursor = index + 1
                        break
                    if stored.ts_ingested < bar.ts_ingested:
                        break
        first_confirmation = False

    # 已落盘的同一发布作为锚点。之前的历史版本是本进程热端编号的基准。
    cursor = matches[0][1] if matches else len(archived)
    previous = archived[cursor - 1] if cursor else None
    revision = previous.revision if previous is not None else -1
    pending: dict[int, RawBar] = {}
    for bar in hot:
        hot_revision = bar.revision
        next_revision, next_index = matches[0] if matches else (-1, len(archived))
        while cursor < next_index and archived[cursor].ts_ingested <= bar.ts_ingested:
            previous = archived[cursor]
            revision = max(revision, previous.revision)
            cursor += 1
        if bar.revision == next_revision:
            previous = archived[next_index]
            revision = max(revision, previous.revision)
            cursor = next_index + 1
            matches.popleft()
            continue
        if bar.synthetic or not bar.confirmed:
            if not include_open:
                revision = max(bar.revision, revision + 1)
                continue
            bar = replace(bar, confirmed=False, ts_available=max(bar.ts_event, bar.ts_ingested))
        elif bar.history_only:
            if previous is not None or archived:
                continue
        elif previous is not None:
            if _values(bar) == _values(previous):
                continue
            bar = replace(bar, ts_available=max(bar.ts_available, bar.ts_ingested, previous.ts_available))
        revision = max(bar.revision, revision + 1)
        bar = replace(bar, revision=revision)
        pending[hot_revision] = bar
        if bar.confirmed:
            previous = bar
    return pending


def _pending_revisions(
    cold: Sequence[RawBar], hot: Sequence[RawBar], *, include_open: bool = False,
) -> list[RawBar]:
    pending: list[RawBar] = []
    hot_by_event = {ts: list(group) for ts, group in groupby(hot, key=lambda bar: bar.ts_event)}
    cold_by_event: dict[int, list[RawBar]] = {}
    for bar in cold:
        if bar.ts_event in hot_by_event:
            cold_by_event.setdefault(bar.ts_event, []).append(bar)
    for ts, versions in hot_by_event.items():
        pending.extend(_reconcile_revisions(cold_by_event.get(ts, []), versions, include_open=include_open).values())
    return pending


def revision_window(
    cold: Sequence[RawBar], hot: Sequence[RawBar], *, since: int, as_of: int | None,
) -> tuple[list[RawBar], int | None]:
    """同一份确认修订用于 HTTP 与订阅，同时报告下一次可见性变化的时刻。"""
    visible = []
    next_available = None
    for bar in chain(cold, _pending_revisions(cold, hot)):
        if not bar.confirmed or bar.synthetic or bar.ts_available < since:
            continue
        if as_of is None or bar.ts_available <= as_of:
            visible.append(bar)
        elif next_available is None or bar.ts_available < next_available:
            next_available = bar.ts_available
    visible.sort(key=lambda bar: (bar.ts_event, bar.revision))
    return visible, next_available


def range_visible_bars(
    cold: Sequence[RawBar], hot: Sequence[RawBar], *, as_of: int | None, include_open: bool,
) -> list[RawBar]:
    """按同一时点规则选版，热端输入按事件时间与修订升序。"""
    pending = _pending_revisions(cold, hot, include_open=include_open)
    closed = latest_visible_bars(chain(cold, (bar for bar in pending if bar.confirmed)), as_of=as_of)
    if not include_open:
        return closed
    closed_events = {bar.ts_event for bar in closed}
    open_bars = latest_visible_bars(
        (
            bar for bar in pending
            if not bar.confirmed and not bar.synthetic and bar.ts_event not in closed_events
        ),
        as_of=as_of,
    )
    real_events = closed_events.union(bar.ts_event for bar in open_bars)
    fallback = latest_visible_bars(
        (bar for bar in pending if bar.synthetic and bar.ts_event not in real_events), as_of=as_of,
    )
    return sorted((*closed, *open_bars, *fallback), key=lambda bar: bar.ts_event)


def read_bar_range(
    reader: ColdReader, hot_buffer: HotStreamBuffer | None, instrument: InstrumentSpec,
    timeframe: str, start: int, end: int | None, *, slot: HotSlot | None = None,
) -> tuple[ColdManifest, list[RawBar], list[RawBar]]:
    """从同一版清单读取冷库，并保留读清单期间可能淘汰或刚落盘的热版本。"""
    def read_hot() -> list[RawBar]:
        if hot_buffer is None:
            return []
        return hot_buffer.revisions(
            instrument_id=instrument.instrument_id, timeframe=timeframe, start_ts=start, end_ts=end,
        )

    before = read_hot() if slot is None else slot.revisions()
    manifest, cold = reader.read_manifest_snapshot(
        instrument, timeframe, start_ts=start, end_ts=end,
    )
    after = read_hot()
    if before == after:
        return manifest, cold, before
    hot = {(bar.ts_event, bar.revision): bar for versions in (before, after) for bar in versions}
    return manifest, cold, [hot[key] for key in sorted(hot)]
