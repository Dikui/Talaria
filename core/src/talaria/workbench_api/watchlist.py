"""Watchlist 业务层：分组/条目持久化、订阅配额与行情展示。

写操作全部经 ``StateWriterQueue.run`` 在单一串行协程中执行；行情读取优先使用
``FeedSupervisor`` 已接收的最近 1 分钟柱，没有实时订阅时回退到最近快照/fixture
最新价，并明确标注来源。STALE 只由 ``FeedHealth`` 判定，不用报价时间猜测。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from talaria.feed.supervisor import FeedSupervisor
from talaria.foundation.errors import (
    FeedUnavailableError,
    InstrumentNotFoundError,
    InvalidRequestError,
    NotFoundError,
)
from talaria.foundation.feed import FeedHealthStatus, worst_status
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.watchlist import (
    DegradationReason,
    QuoteSource,
    Watchlist,
    WatchlistGroup,
    WatchlistGroupKind,
    WatchlistItem,
)
from talaria.workbench_api.state_store import (
    StateStore,
    StateWriterQueue,
    WatchlistGroupRecord,
    WatchlistItemRecord,
    WatchlistRecords,
)
from talaria.workbench_api.subscriptions import (
    WATCHLIST_TIMEFRAME,
    RealtimeSubscriptionScheduler,
    SubscriptionView,
)

QUOTE_CACHE_TTL_MS = 5_000
CHANGE_QUANT = Decimal("0.01")


@dataclass(frozen=True)
class _Quote:
    latest_price: Decimal | None
    change_pct: Decimal | None
    quote_ts: int | None
    source: QuoteSource


_NO_QUOTE = _Quote(
    latest_price=None,
    change_pct=None,
    quote_ts=None,
    source=QuoteSource.UNAVAILABLE,
)


def _change_pct(latest: Decimal, previous: Decimal) -> Decimal | None:
    if previous == 0:
        return None
    return ((latest - previous) / previous * Decimal(100)).quantize(CHANGE_QUANT)


class WatchlistService:
    """面向 API 的 Watchlist 聚合服务。"""

    def __init__(
        self,
        *,
        state_writer: StateWriterQueue,
        instruments: Sequence[InstrumentSpec],
        connector: Any | None,
        supervisor: FeedSupervisor | None,
        scheduler: RealtimeSubscriptionScheduler | None,
        now_ms: Callable[[], int],
    ) -> None:
        self._state_writer = state_writer
        self._instruments = {spec.instrument_id: spec for spec in instruments}
        self._connector = connector
        self._supervisor = supervisor
        self._scheduler = scheduler
        self._now_ms = now_ms
        self._quote_cache: dict[str, tuple[int, _Quote]] = {}

    # ------------------------------------------------------------------ 读取

    async def get_watchlist(self) -> Watchlist:
        records = await self._load_records()
        ordered = [
            item.instrument_id
            for group in records.groups
            for item in records.items_for(group.group_id)
        ]
        view, statuses = self._reconcile(ordered)
        quotes = await self._resolve_quotes(ordered, view)
        return self._render(records, view, statuses, quotes)

    async def rebalance(self) -> None:
        """图表订阅变化后重新分配配额；WS 退订/关闭时调用。"""
        records = await self._load_records()
        ordered = [
            item.instrument_id
            for group in records.groups
            for item in records.items_for(group.group_id)
        ]
        self._reconcile(ordered)

    async def _load_records(self) -> WatchlistRecords:
        return await self._state_writer.run(lambda store: store.list_watchlist())

    def _reconcile(
        self, ordered: Sequence[str]
    ) -> tuple[SubscriptionView, dict[str, FeedHealthStatus]]:
        if self._scheduler is None:
            view = SubscriptionView(
                selected=frozenset(),
                subscribed=frozenset(),
                degraded={
                    instrument_id: DegradationReason.NO_FEED
                    for instrument_id in dict.fromkeys(ordered)
                },
                chart_instruments=frozenset(),
            )
        else:
            view = self._scheduler.reconcile(ordered)

        statuses: dict[str, list[FeedHealthStatus]] = {}
        if self._supervisor is not None:
            for channel in self._supervisor.health().channels:
                statuses.setdefault(channel.instrument_id, []).append(channel.status)
        return view, {
            instrument_id: worst_status(values)
            for instrument_id, values in statuses.items()
        }

    async def _resolve_quotes(
        self,
        ordered: Sequence[str],
        view: SubscriptionView,
    ) -> dict[str, _Quote]:
        unique = list(dict.fromkeys(ordered))
        resolved = await asyncio.gather(
            *(self._quote(instrument_id, view) for instrument_id in unique)
        )
        return dict(zip(unique, resolved, strict=True))

    async def _quote(self, instrument_id: str, view: SubscriptionView) -> _Quote:
        if instrument_id in view.selected and self._supervisor is not None:
            live = self._live_quote(instrument_id)
            if live is not None:
                return live
        return await asyncio.to_thread(self._snapshot_quote, instrument_id)

    def _live_quote(self, instrument_id: str) -> _Quote | None:
        assert self._supervisor is not None
        bars = self._supervisor.recent_bars(
            instrument_id=instrument_id, timeframe=WATCHLIST_TIMEFRAME
        )
        closes = [bar for bar in bars if bar.close is not None]
        if not closes:
            return None
        latest = closes[-1]
        previous = closes[-2] if len(closes) > 1 else None
        return _Quote(
            latest_price=latest.close,
            change_pct=(
                _change_pct(latest.close, previous.close)
                if previous is not None and previous.close is not None
                else None
            ),
            quote_ts=latest.timestamp,
            source=QuoteSource.SUPERVISOR,
        )

    def _snapshot_quote(self, instrument_id: str) -> _Quote:
        now = int(self._now_ms())
        cached = self._quote_cache.get(instrument_id)
        if cached is not None and now - cached[0] < QUOTE_CACHE_TTL_MS:
            return cached[1]

        spec = self._instruments.get(instrument_id)
        quote = _NO_QUOTE
        if spec is not None and self._connector is not None:
            try:
                bars = self._connector.fetch_bars(
                    symbol=spec.symbol,
                    timeframe=WATCHLIST_TIMEFRAME,
                    limit=2,
                )
            except FeedUnavailableError:
                bars = ()
            closes = [bar for bar in bars if bar.close is not None]
            if closes:
                latest = closes[-1]
                previous = closes[-2] if len(closes) > 1 else None
                quote = _Quote(
                    latest_price=latest.close,
                    change_pct=(
                        _change_pct(latest.close, previous.close)
                        if previous is not None and previous.close is not None
                        else None
                    ),
                    quote_ts=latest.ts_event,
                    source=QuoteSource.SNAPSHOT,
                )
        self._quote_cache[instrument_id] = (now, quote)
        return quote

    def _render(
        self,
        records: WatchlistRecords,
        view: SubscriptionView,
        statuses: dict[str, FeedHealthStatus],
        quotes: dict[str, _Quote],
    ) -> Watchlist:
        groups: list[WatchlistGroup] = []
        for group_position, group in enumerate(records.groups):
            items = [
                self._render_item(
                    record, item_position, view, statuses, quotes
                )
                for item_position, record in enumerate(
                    records.items_for(group.group_id)
                )
            ]
            groups.append(
                WatchlistGroup(
                    group_id=group.group_id,
                    name=group.name,
                    position=group_position,
                    kind=WatchlistGroupKind(group.kind),
                    is_system=group.is_system,
                    items=items,
                )
            )
        return Watchlist(
            groups=groups,
            realtime_quota=self._scheduler.quota if self._scheduler is not None else 0,
            realtime_used=self._scheduler.used if self._scheduler is not None else 0,
            generated_ts=int(self._now_ms()),
        )

    def _render_item(
        self,
        record: WatchlistItemRecord,
        position: int,
        view: SubscriptionView,
        statuses: dict[str, FeedHealthStatus],
        quotes: dict[str, _Quote],
    ) -> WatchlistItem:
        quote = quotes.get(record.instrument_id, _NO_QUOTE)
        feed_status = statuses.get(record.instrument_id)
        reason = view.degraded.get(record.instrument_id)
        degraded = reason is not None
        return WatchlistItem(
            instrument_id=record.instrument_id,
            position=position,
            latest_price=quote.latest_price,
            change_pct=quote.change_pct,
            quote_ts=quote.quote_ts,
            quote_source=quote.source,
            realtime=record.instrument_id in view.selected,
            feed_status=feed_status,
            stale=feed_status is FeedHealthStatus.STALE,
            degraded=degraded,
            degradation_reason=reason,
        )

    # ------------------------------------------------------------------ 分组写操作

    async def create_group(self, name: str) -> Watchlist:
        await self._state_writer.run(
            lambda store: store.create_watchlist_group(name)
        )
        return await self.get_watchlist()

    async def rename_group(self, group_id: str, name: str) -> Watchlist:
        def operation(store: StateStore) -> None:
            self._require_manual_group(store, group_id)
            store.rename_watchlist_group(group_id, name)

        await self._state_writer.run(operation)
        return await self.get_watchlist()

    async def delete_group(self, group_id: str) -> Watchlist:
        def operation(store: StateStore) -> None:
            self._require_manual_group(store, group_id)
            store.delete_watchlist_group(group_id)

        await self._state_writer.run(operation)
        return await self.get_watchlist()

    async def order_groups(self, ids: list[str]) -> Watchlist:
        def operation(store: StateStore) -> None:
            records = store.list_watchlist()
            all_ids = [group.group_id for group in records.groups]
            if len(ids) != len(set(ids)) or set(ids) != set(all_ids):
                raise InvalidRequestError("排序必须覆盖全部分组且不能重复")
            system_ids = {
                group.group_id
                for group in records.groups
                if group.is_system
            }
            for index, group_id in enumerate(ids):
                if group_id in system_ids and all_ids[index] != group_id:
                    raise InvalidRequestError("系统分组不可调整顺序")
            manual_ids = [group_id for group_id in ids if group_id not in system_ids]
            store.reorder_watchlist_groups(manual_ids)

        await self._state_writer.run(operation)
        return await self.get_watchlist()

    # ------------------------------------------------------------------ 条目写操作

    async def add_item(self, group_id: str, instrument_id: str) -> Watchlist:
        if instrument_id not in self._instruments:
            raise InstrumentNotFoundError(f"未知标的：{instrument_id}")

        def operation(store: StateStore) -> None:
            self._require_manual_group(store, group_id)
            try:
                store.add_watchlist_item(group_id, instrument_id)
            except ValueError as exc:
                raise InvalidRequestError(str(exc)) from exc

        await self._state_writer.run(operation)
        return await self.get_watchlist()

    async def remove_item(self, instrument_id: str) -> Watchlist:
        """按契约从所有手工分组移除该标的；系统分组只读，不能被删除。"""

        def operation(store: StateStore) -> None:
            records = store.list_watchlist()
            manual_groups = {
                group.group_id
                for group in records.groups
                if not group.is_system
            }
            removable = [
                item
                for item in records.items
                if item.instrument_id == instrument_id
                and item.group_id in manual_groups
            ]
            if not removable:
                if any(
                    item.instrument_id == instrument_id
                    for item in records.items
                    if item.group_id not in manual_groups
                ):
                    raise InvalidRequestError("系统分组只读")
                raise NotFoundError(f"标的未加入自选：{instrument_id}")
            for item in removable:
                store.remove_watchlist_item(item.group_id, instrument_id)

        await self._state_writer.run(operation)
        return await self.get_watchlist()

    async def order_items(self, group_id: str, ids: list[str]) -> Watchlist:
        def operation(store: StateStore) -> None:
            group = self._require_manual_group(store, group_id)
            current = [
                item.instrument_id
                for item in store.list_watchlist().items_for(group.group_id)
            ]
            if len(ids) != len(set(ids)) or set(ids) != set(current):
                raise InvalidRequestError("排序必须覆盖分组内全部标的且不能重复")
            store.reorder_watchlist_items(group_id, ids)

        await self._state_writer.run(operation)
        return await self.get_watchlist()

    @staticmethod
    def _require_manual_group(
        store: StateStore, group_id: str
    ) -> WatchlistGroupRecord:
        group = store.get_watchlist_group(group_id)
        if group is None:
            raise NotFoundError(f"分组不存在：{group_id}")
        if group.is_system or group.kind == WatchlistGroupKind.SCANNER_FOCUS.value:
            raise InvalidRequestError("系统分组只读")
        return group
