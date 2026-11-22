"""按范围接口的可见修订差量建立每通道的策略日志。"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from uuid import uuid4

from talaria.foundation.bars import RangeBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.feed import FeedEvent, KlineEvent
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.strategy_subscription import (
    StrategyBarEvent, StrategyDerivativeEvent, SubscriptionCursor, SubscriptionReady,
    identified_derivative_revisions, identified_revisions,
)
from talaria.perception.bar_range import revision_window, read_bar_range
from talaria.perception.cold_store import ColdReader
from talaria.perception.hot_buffer import HotSlot, HotStreamBuffer
from talaria.workbench_api.subscriptions import RealtimeSubscriptionScheduler, SubscriptionKey

QUEUE_SIZE = 256
LOG_SIZE = 4096
RECONNECT_LEASE_SECONDS = 30
logger = logging.getLogger(__name__)
type EventRange = tuple[int, int | None, HotSlot | None]
type StrategyKey = SubscriptionKey | tuple[str, str, DerivativeKind]
type StrategyEvent = StrategyBarEvent | StrategyDerivativeEvent
FULL_RANGE: EventRange = (0, None, None)


@dataclass(eq=False)
class StrategySession:
    ready: SubscriptionReady
    replay: tuple[StrategyEvent, ...]
    baseline: tuple[str, ...] = ()
    queue: asyncio.Queue[StrategyEvent] = field(
        default_factory=lambda: asyncio.Queue(maxsize=QUEUE_SIZE),
    )
    interrupted: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Lease:
    key: StrategyKey
    initial: SubscriptionCursor
    started_ts: int
    baseline: tuple[str, ...]
    session: StrategySession | None = None
    expiry: asyncio.TimerHandle | None = None


@dataclass
class _Journal:
    epoch: str = field(default_factory=lambda: uuid4().hex)
    seq: int = 0
    events: deque[StrategyEvent] = field(default_factory=lambda: deque(maxlen=LOG_SIZE))
    seen: set[str] = field(default_factory=set)
    clients: set[StrategySession] = field(default_factory=set)
    dirty: dict[EventRange, None] = field(default_factory=dict)
    attaching: bool = False
    worker: asyncio.Task[None] | None = None
    timer: asyncio.TimerHandle | None = None
    next_available: int | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class StrategyStream:
    def __init__(
        self, *, scheduler: RealtimeSubscriptionScheduler, hot_buffer: HotStreamBuffer,
        reader: ColdReader, instruments: Sequence[InstrumentSpec], now_ms: Callable[[], int],
    ) -> None:
        self._scheduler = scheduler
        self._hot_buffer = hot_buffer
        self._reader = reader
        self._instruments = {spec.instrument_id: spec for spec in instruments}
        self._now_ms = now_ms
        self._leases: dict[str, _Lease] = {}
        self._journals: dict[StrategyKey, _Journal] = {}
        self._attaching = asyncio.Lock()

    def ingest(self, event: FeedEvent) -> None:
        raw = self._hot_buffer.ingest(event)
        if raw is not None and raw.confirmed and not raw.synthetic:
            assert isinstance(event, KlineEvent)
            key = (event.instrument_id, event.timeframe)
            if key in self._journals:
                self.changed(key, raw.ts_event, raw.ts_event, self._hot_buffer.slot(key, raw.ts_event))

    def changed(
        self, key: StrategyKey, start: int, end: int | None, slot: HotSlot | None = None,
    ) -> None:
        journal = self._journals.get(key)
        if journal is None:
            return
        # 重叠通知只需比较一次；积压时合并为一次全范围差量读取。
        bounds = (start, end, slot)
        if len(journal.dirty) >= LOG_SIZE and bounds not in journal.dirty:
            journal.dirty.clear()
            self._invalidate(journal)
            journal.dirty[FULL_RANGE] = None
        journal.dirty[bounds] = None
        if journal.worker is None or journal.worker.done():
            journal.worker = asyncio.create_task(self._drain(key, journal))

    def derivative_changed(self, instrument: str, kind: DerivativeKind, start: int, end: int) -> None:
        for key in self._journals:
            if len(key) == 3 and key[0] == instrument and key[2] is kind:
                self.changed(key, start, end)

    def _read(
        self, key: StrategyKey, bounds: EventRange, now: int,
    ) -> tuple[list[tuple[str, RangeBar | DerivativePoint]], int | None]:
        start, end, slot = bounds
        if len(key) == 3:
            points = self._reader.read_derivative_revisions(
                self._instruments[key[0]], key[2], start_ts=start, end_ts=end,
            )
            future = min((point.ts_available for point in points if point.ts_available > now), default=None)
            rows = identified_derivative_revisions(point for point in points if point.ts_available <= now)
            return list(rows), future
        _, cold, hot = read_bar_range(
            self._reader, self._hot_buffer, self._instruments[key[0]], key[1], start, end, slot=slot,
        )
        rows, future = revision_window(cold, hot, since=0, as_of=now)
        bars = (RangeBar.model_validate(row, from_attributes=True) for row in rows)
        return list(identified_revisions(bars)), future

    async def _refresh(
        self, key: StrategyKey, journal: _Journal, bounds: EventRange, *, baseline: bool = False,
    ) -> None:
        now = self._now_ms()
        rows, future = await asyncio.to_thread(self._read, key, bounds, now)
        for identity, bar in rows:
            if identity in journal.seen:
                continue
            journal.seen.add(identity)
            if not baseline:
                self._publish(key, journal, identity, bar)
        if future is not None and (journal.next_available is None or future < journal.next_available):
            if journal.timer is not None:
                journal.timer.cancel()
            journal.next_available = future
            self._wait_for_visibility(key, journal, future)

    def _wait_for_visibility(self, key: StrategyKey, journal: _Journal, due: int) -> None:
        remaining = (due - self._now_ms()) / 1000
        if remaining <= 0:
            journal.timer = None
            journal.next_available = None
            self.changed(key, 0, None)
        else:
            # 回放时钟可以跳跃或暂停，真正到期才重读范围。
            journal.timer = asyncio.get_running_loop().call_later(
                min(.25, remaining), self._wait_for_visibility, key, journal, due,
            )

    async def _drain(self, key: StrategyKey, journal: _Journal) -> None:
        try:
            while journal.dirty:
                async with journal.lock:
                    bounds = next(iter(journal.dirty))
                    del journal.dirty[bounds]
                    await self._refresh(key, journal, bounds)
        except Exception:
            logger.exception("读取策略订阅范围失败：%s", key)
            self._invalidate(journal)

    @staticmethod
    def _invalidate(journal: _Journal) -> None:
        journal.epoch = uuid4().hex
        journal.seq = 0
        journal.events.clear()
        for session in journal.clients:
            session.interrupted.set()
        journal.clients.clear()

    async def attach(
        self, owner: str, key: StrategyKey, after: SubscriptionCursor | None,
        *, include_baseline: bool = False, report_replay_through: bool = False,
    ) -> StrategySession:
        async with self._attaching:
            upstream = (key[0], key[1])
            self._scheduler.check_account(owner, upstream)
            journal = self._journals.get(key)
            fresh = journal is None
            if journal is None:
                journal = self._journals[key] = _Journal()
            journal.attaching = True
            try:
                async with journal.lock:
                    lease = self._leases.get(owner)
                    if lease is not None and lease.key != key:
                        raise ValueError("连接所有者已持有另一个策略订阅")
                    if lease is not None and lease.expiry is not None:
                        lease.expiry.cancel()
                        lease.expiry = None
                    if lease is None:
                        # 初次挂载以本次范围快照和对应日志序号为界。读取期间的通知已排队。
                        started_ts = self._now_ms()
                        await self._refresh(key, journal, FULL_RANGE, baseline=fresh)
                        self._scheduler.acquire_account(owner, upstream)
                        initial = SubscriptionCursor(epoch=journal.epoch, seq=journal.seq)
                        baseline = tuple(journal.seen) if include_baseline and after is None else ()
                        lease = _Lease(key, initial, started_ts, baseline)
                        self._leases[owner] = lease
                    if lease.session is not None:
                        journal.clients.discard(lease.session)
                        lease.session.interrupted.set()
                    cursor = after or lease.initial
                    oldest = journal.events[0].seq if journal.events else journal.seq + 1
                    assert oldest is not None
                    valid = cursor.epoch == journal.epoch and oldest - 1 <= cursor.seq <= journal.seq
                    baseline = lease.baseline if after is None else ()
                    session = StrategySession(
                        SubscriptionReady(
                            type="ready" if valid else "resync", epoch=journal.epoch,
                            seq=cursor.seq if valid else journal.seq, server_ts=self._now_ms(),
                            started_ts=lease.started_ts, baseline_count=len(baseline),
                            replay_through=journal.seq if report_replay_through else None,
                        ),
                        tuple(ev for ev in journal.events if valid and ev.seq > cursor.seq),
                        baseline,
                    )
                    journal.clients.add(session)
                    lease.session = session
                    return session
            finally:
                journal.attaching = False
                self._discard_unused(key)

    def detach(self, owner: str, session: StrategySession, *, release: bool) -> None:
        lease = self._leases.get(owner)
        if lease is None or lease.session is not session:
            return
        self._journals[lease.key].clients.discard(session)
        lease.session = None
        if release:
            self._release(owner)
        else:
            lease.expiry = asyncio.get_running_loop().call_later(
                RECONNECT_LEASE_SECONDS, self._release, owner,
            )

    def close(self) -> None:
        for owner in tuple(self._leases):
            self._release(owner)

    def _release(self, owner: str) -> None:
        lease = self._leases.pop(owner)
        if lease.expiry is not None:
            lease.expiry.cancel()
        if lease.session is not None:
            lease.session.interrupted.set()
            self._journals[lease.key].clients.discard(lease.session)
        self._scheduler.release_account(owner)
        self._discard_unused(lease.key)

    def _discard_unused(self, key: StrategyKey) -> None:
        if any(lease.key == key for lease in self._leases.values()):
            return
        journal = self._journals.get(key)
        if journal is None:
            return
        if journal.attaching:
            self._invalidate(journal)
            return
        self._journals.pop(key)
        if journal.worker is not None:
            journal.worker.cancel()
        if journal.timer is not None:
            journal.timer.cancel()

    @staticmethod
    def _publish(
        key: StrategyKey, journal: _Journal, identity: str, row: RangeBar | DerivativePoint,
    ) -> None:
        journal.seq += 1
        if isinstance(row, DerivativePoint):
            event = StrategyDerivativeEvent(
                epoch=journal.epoch, seq=journal.seq, point=row, content_id=identity,
            )
        else:
            event = StrategyBarEvent(
                instrument_id=key[0], timeframe=key[1], epoch=journal.epoch, seq=journal.seq,
                bar=row, content_id=identity,
            )
        journal.events.append(event)
        for session in tuple(journal.clients):
            if session.queue.full():
                journal.clients.remove(session)
                session.interrupted.set()
            else:
                session.queue.put_nowait(event)
