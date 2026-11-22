"""首次回填：按 end 向左分页，直到攒够目标根数或达到页数上限。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from talaria.foundation.connector import MarketDataConnector, RawBar
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
