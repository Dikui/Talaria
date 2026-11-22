"""常驻服务的策略变更日志、实时缓冲与账户订阅租约。"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from uuid import uuid4

from talaria.foundation.bars import RangeBar
from talaria.foundation.connector import RawBar
from talaria.foundation.feed import FeedEvent, KlineEvent
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.strategy_subscription import (
    StrategyBarEvent,
    SubscriptionCursor,
    SubscriptionReady,
)
from talaria.perception.bar_range import reconciled_hot_revision
from talaria.perception.cold_store import ColdReader
from talaria.perception.hot_buffer import HotStreamBuffer
from talaria.workbench_api.subscriptions import RealtimeSubscriptionScheduler, SubscriptionKey

QUEUE_SIZE = 256
LOG_SIZE = 4096
RECONNECT_LEASE_SECONDS = 30
logger = logging.getLogger(__name__)


@dataclass(eq=False)
class StrategySession:
    ready: SubscriptionReady
    replay: tuple[StrategyBarEvent, ...]
    queue: asyncio.Queue[StrategyBarEvent] = field(
        default_factory=lambda: asyncio.Queue(maxsize=QUEUE_SIZE),
    )
    interrupted: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Lease:
    key: SubscriptionKey
    initial: SubscriptionCursor
    started_ts: int
    session: StrategySession | None = None
    expiry: asyncio.TimerHandle | None = None


@dataclass
class _Journal:
    seq: int = 0
    interruptions: int = 0
    events: deque[StrategyBarEvent] = field(default_factory=lambda: deque(maxlen=LOG_SIZE))
    clients: set[StrategySession] = field(default_factory=set)


class StrategyStream:
    def __init__(
        self,
        *,
        scheduler: RealtimeSubscriptionScheduler,
        hot_buffer: HotStreamBuffer,
        reader: ColdReader,
        instruments: Sequence[InstrumentSpec],
        now_ms: Callable[[], int],
    ) -> None:
        self._scheduler = scheduler
        self._hot_buffer = hot_buffer
        self._reader = reader
        self._instruments = {spec.instrument_id: spec for spec in instruments}
        self._now_ms = now_ms
        self._epoch = uuid4().hex
        self._leases: dict[str, _Lease] = {}
        self._journals: dict[SubscriptionKey, _Journal] = {}
        self._pending: asyncio.Queue[tuple[SubscriptionKey, list[RawBar]]] = asyncio.Queue()

    def ingest(self, event: FeedEvent) -> None:
        raw = self._hot_buffer.ingest(event)
        if raw is None or not raw.confirmed or raw.synthetic:
            return
        assert isinstance(event, KlineEvent)
        key = (event.instrument_id, event.timeframe)
        if key not in self._journals:
            return
        versions = self._hot_buffer.revisions(
            instrument_id=key[0], timeframe=key[1],
            start_ts=raw.ts_event, end_ts=raw.ts_event,
        )
        if not versions:
            # 很早的修订可能在插入热缓冲时立即被淘汰，仍应进入变更日志。
            versions = [raw]
        self._pending.put_nowait((key, versions))

    async def run(self) -> None:
        while True:
            key, versions = await self._pending.get()
            raw = versions[-1]
            try:
                cold = await asyncio.to_thread(
                    self._reader.read_revisions, self._instruments[key[0]], key[1],
                    start_ts=raw.ts_event, end_ts=raw.ts_event,
                )
                # 冷库可能已经写入较新的柱修订，加入对应热端版本作为编号锚点。
                later = self._hot_buffer.revisions(
                    instrument_id=key[0], timeframe=key[1],
                    start_ts=raw.ts_event, end_ts=raw.ts_event,
                )
                versions.extend(bar for bar in later if bar.revision > raw.revision)
                version = reconciled_hot_revision(cold, versions, raw.revision)
            except Exception:
                # a client must resync after a failed canonical read
                self._invalidate()
                logger.exception("无法读取实时柱的冷库修订：%s %s", *key)
                continue
            if version is not None:
                self._publish(key, RangeBar.model_validate(version, from_attributes=True))

    def attach(
        self, owner: str, key: SubscriptionKey, after: SubscriptionCursor | None,
    ) -> StrategySession:
        journal = self._journal(key)
        lease = self._leases.get(owner)
        now = self._now_ms()
        if lease is None:
            self._scheduler.acquire_account(owner, key)
            lease = _Lease(key, SubscriptionCursor(epoch=self._epoch, seq=journal.seq), now)
            self._leases[owner] = lease
        elif lease.key != key:
            raise ValueError("同一账户订阅标识只能持有一个通道")
        if lease.expiry is not None:
            lease.expiry.cancel()
            lease.expiry = None
        if lease.session is not None:
            journal.clients.discard(lease.session)
            lease.session.interrupted.set()

        # 在同一个事件循环步内先挂实时队列，再取日志快照。
        session = StrategySession(
            SubscriptionReady(
                type="ready", epoch=self._epoch, seq=journal.seq, server_ts=now,
                started_ts=lease.started_ts,
            ),
            (),
        )
        journal.clients.add(session)
        lease.session = session
        cursor = after or lease.initial
        oldest = journal.events[0].seq if journal.events else journal.seq + 1
        assert oldest is not None
        if cursor.epoch != self._epoch or not oldest - 1 <= cursor.seq <= journal.seq:
            session.ready = session.ready.model_copy(update={"type": "resync"})
        else:
            session.ready = session.ready.model_copy(update={"seq": cursor.seq})
            session.replay = tuple(
                event for event in journal.events if event.seq is not None and event.seq > cursor.seq
            )
        return session

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
            self._journals[lease.key].clients.discard(lease.session)
            lease.session.interrupted.set()
        self._scheduler.release_account(owner)

    def _publish(self, key: SubscriptionKey, bar: RangeBar) -> None:
        journal = self._journal(key)
        journal.seq += 1
        event = StrategyBarEvent(
            instrument_id=key[0], timeframe=key[1], epoch=self._epoch, seq=journal.seq, bar=bar,
        )
        journal.events.append(event)
        for session in tuple(journal.clients):
            if session.queue.full():
                journal.clients.remove(session)
                session.interrupted.set()
            else:
                session.queue.put_nowait(event)

    def _journal(self, key: SubscriptionKey) -> _Journal:
        interruptions = self._scheduler.interruptions(key)
        journal = self._journals.get(key)
        if journal is None:
            journal = self._journals[key] = _Journal(interruptions=interruptions)
        elif journal.interruptions != interruptions:
            # 退订期间没有上游事件，旧游标即使还在保留范围也需要重同步。
            journal.interruptions = interruptions
            journal.seq += 1
            journal.events.clear()
        return journal

    def _invalidate(self) -> None:
        self._epoch = uuid4().hex
        for journal in self._journals.values():
            journal.events.clear()
            journal.seq = 0
            for session in journal.clients:
                session.interrupted.set()
            journal.clients.clear()
