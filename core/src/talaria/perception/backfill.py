"""首次回填：按 end 向左分页，直到攒够目标根数或达到页数上限。"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace

from talaria.foundation.connector import MarketDataConnector, RawBar
from talaria.foundation.derivatives import (
    OI_TIMEFRAME_BY_TIMEFRAME, DerivativeDataConnector, DerivativeKind, RawDerivativePoint,
)
from talaria.perception.raw_store import RawStore, latest_visible_bars


@dataclass(frozen=True, slots=True)
class BackfillPolicy:
    target_bars: int = 3000
    max_pages: int = 5
    page_limit: int = 1000

    def __post_init__(self) -> None:
        if self.target_bars <= 0:
            raise ValueError("backfill_bars 必须为正数")
        if self.max_pages <= 0:
            raise ValueError("max_pages 必须为正数")
        if not 1 <= self.page_limit <= 1000:
            raise ValueError("单页上限必须在 1..1000")


def ensure_history(
    *,
    raw_store: RawStore,
    connector: MarketDataConnector,
    symbol: str,
    timeframe: str,
    cutoff_ts: int,
    policy: BackfillPolicy,
    cold_bars: Sequence[RawBar] = (),
) -> list[RawBar]:
    """确保本地至少缓存 ``target_bars`` 根可用柱，返回可用柱（升序）。"""

    def usable() -> list[RawBar]:
        return latest_visible_bars(
            [*cold_bars, *raw_store.revisions(symbol, timeframe)], as_of=cutoff_ts
        )

    existing = usable()
    pages = 0
    while len(existing) < policy.target_bars and pages < policy.max_pages:
        end_ts = existing[0].ts_event - 1 if existing else cutoff_ts
        wanted = min(policy.page_limit, policy.target_bars - len(existing))
        fetched = list(
            connector.fetch_bars(
                symbol=symbol,
                timeframe=timeframe,
                end_ts=end_ts,
                limit=wanted,
            )
        )
        oldest = existing[0].ts_event if existing else None
        fresh = [
            bar
            for bar in fetched
            if bar.ts_available <= cutoff_ts and (oldest is None or bar.ts_event < oldest)
        ]
        if not fresh:
            break
        raw_store.append(symbol, timeframe, fresh)
        pages += 1
        existing = usable()
    return existing


def derivative_series(timeframes: Sequence[str]) -> tuple[tuple[DerivativeKind, str | None], ...]:
    return (
        *((DerivativeKind.MARK_PRICE, tf) for tf in sorted(set(timeframes))),
        (DerivativeKind.FUNDING_RATE, None),
        *((DerivativeKind.OPEN_INTEREST, tf) for tf in sorted({
            OI_TIMEFRAME_BY_TIMEFRAME[tf] for tf in timeframes if tf in OI_TIMEFRAME_BY_TIMEFRAME
        })),
    )


def derivative_history_pages(
    connector: DerivativeDataConnector, policy: BackfillPolicy, *, symbol: str,
    kind: DerivativeKind, timeframe: str | None, cutoff: int,
    start: int | None = None, end: int | None = None,
) -> Iterator[list[RawDerivativePoint]]:
    """从右向左分页。快照保留达到目标时已获取的整页。"""
    cursor = cutoff if end is None else min(end, cutoff)
    lower = 0 if start is None else start
    target = policy.target_bars
    if kind is DerivativeKind.MARK_PRICE:
        assert timeframe is not None
        limit = policy.page_limit
        fetch = connector.fetch_mark_price_bars
    elif kind is DerivativeKind.OPEN_INTEREST:
        assert timeframe is not None
        limit = 200
        fetch = connector.fetch_open_interest
    else:
        limit = 200
        target = max(8, min(1000, target // 8))
        fetch = connector.fetch_funding_settlements

    kwargs = {} if timeframe is None else {"timeframe": timeframe}
    collected = 0
    pages = 0
    while cursor >= lower and (start is not None or pages < policy.max_pages):
        pages += 1
        batch = {
            row.ts_event: row for row in fetch(symbol=symbol, end_ts=cursor, limit=limit, **kwargs)
            if row.kind is kind and row.timeframe == timeframe and row.symbol == symbol and row.ts_event <= cursor
        }
        if kind is DerivativeKind.FUNDING_RATE:
            # REST 历史给出实际结算。更正的可获取时间由写入器按接收时刻确定。
            batch = {ts: replace(row, ts_available=ts) for ts, row in batch.items()}
        visible = [row for row in batch.values() if row.ts_event >= lower and row.ts_available <= cutoff]
        if visible:
            yield visible
        collected += len(visible)
        if start is None:
            if len(visible) < limit or collected >= target:
                break
        elif len(batch) < limit:
            break
        cursor = min(batch) - 1
