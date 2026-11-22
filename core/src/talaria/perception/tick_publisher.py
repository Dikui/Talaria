"""逐笔与买一卖一批量发布到常驻服务的冷库写入线程。"""

import asyncio
import logging
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date
from itertools import batched, chain, groupby

from talaria.foundation.feed import FeedEvent, OrderBookEvent, ReceivedOrderBookEvent, TradeEvent
from talaria.foundation.market import InstrumentSpec, TradeArchiveReader
from talaria.foundation.ticks import Quote, TickKind, Trade, tick_identity, tick_payload
from talaria.perception.cold_store import (
    TICK_BATCH_ROWS, ColdStore, partition_day, tick_column_scales, validate_tick,
)

logger = logging.getLogger(__name__)
TICK_FLUSH_SECONDS = 60
TICK_WRITE_ATTEMPTS = 3
TICK_RETRY_BATCHES = 16


@dataclass
class _PendingBatch:
    instrument: InstrumentSpec
    kind: TickKind
    events: tuple[TradeEvent | OrderBookEvent, ...]
    failures: int = 0


class TickPublisher:
    def __init__(
        self, store: ColdStore, instruments: Sequence[InstrumentSpec],
        now_ms: Callable[[], int], writer: ThreadPoolExecutor, archive_reader: TradeArchiveReader | None,
    ) -> None:
        self._store = store
        self._instruments = {spec.instrument_id: spec for spec in instruments}
        self._now_ms = now_ms
        self._writer = writer
        self._archive_reader = archive_reader
        self._retries: deque[_PendingBatch] = deque()  # 只由写入线程访问。
        self._last_flush: Future[bool] | None = None
        self._pending: dict[tuple[str, TickKind], list[TradeEvent | OrderBookEvent]] = defaultdict(list)

    def ingest(self, event: FeedEvent) -> None:
        if isinstance(event, (TradeEvent, OrderBookEvent)) and event.instrument_id in self._instruments:
            kind = TickKind.TRADES if isinstance(event, TradeEvent) else TickKind.QUOTES
            self._pending[event.instrument_id, kind].append(event)

    async def flush_periodically(self) -> None:
        while True:
            await asyncio.sleep(TICK_FLUSH_SECONDS)
            await self.flush()

    async def flush(self, *, drain: bool = False) -> None:
        pending = self._pending
        last = self._last_flush
        if not pending and (last is None or (last.done() and not last.result())):
            return
        # 取消定时任务时，写入线程仍保留已提交的批次和重试状态。
        self._last_flush = self._writer.submit(self._flush_batches, pending, drain)
        self._pending = defaultdict(list)
        await asyncio.shield(asyncio.wrap_future(self._last_flush))

    def _flush_batches(
        self, pending: dict[tuple[str, TickKind], list[TradeEvent | OrderBookEvent]], drain: bool,
    ) -> bool:
        batches = (
            _PendingBatch(self._instruments[instrument], kind, batch)
            for (instrument, kind), events in pending.items()
            for _, day_events in groupby(events, key=lambda event: partition_day(event.ts_event))
            for batch in batched(day_events, TICK_BATCH_ROWS)
        )
        self._write_batches(batches)
        while drain and self._retries:
            self._write_batches(())
        return bool(self._retries)

    def _write_batches(self, batches: Iterable[_PendingBatch]) -> None:
        queued, self._retries = self._retries, deque()
        blocked = set()
        for batch in chain(queued, batches):
            series = batch.instrument.instrument_id, batch.kind
            if series not in blocked:
                try:
                    self._publish(batch.instrument, batch.kind, batch.events)
                except Exception:
                    batch.failures += 1
                    logger.warning("实时行情冷库批次写入失败（%s %s）", *series, exc_info=True)
                else:
                    continue
            if batch.failures >= TICK_WRITE_ATTEMPTS:
                self._log_gap(batch.instrument, batch.kind, batch.events, "重试耗尽")
            elif len(self._retries) >= TICK_RETRY_BATCHES:
                self._log_gap(batch.instrument, batch.kind, batch.events, "重试队列已满")
            else:
                self._retries.append(batch)
                # 同一序列后续批次等前一批次完成，避免把旧版本误作修订。
                blocked.add(series)

    @staticmethod
    def _log_gap(
        instrument: InstrumentSpec, kind: TickKind, rows: Sequence[TradeEvent | OrderBookEvent | Trade],
        reason: str,
    ) -> None:
        logger.warning(
            "行情冷库缺口（%s %s）事件时间 [%d, %d]，%d 条，%s",
            instrument.instrument_id, kind,
            min(row.ts_event for row in rows), max(row.ts_event for row in rows), len(rows), reason,
        )

    def _publish(
        self, spec: InstrumentSpec, kind: TickKind, events: Sequence[TradeEvent | OrderBookEvent],
    ) -> None:
        if kind is TickKind.TRADES:
            existing = [
                row for row in self._store.read_trade_revisions_by_id(
                    spec, {event.trade_id.bytes for event in events if isinstance(event, TradeEvent)},
                ) if row.availability == "measured"
            ]
        else:
            event_times = {event.ts_event for event in events}
            existing = self._store.read_tick_revisions(
                spec, kind, start_ts=min(event_times), end_ts=max(event_times), event_times=event_times,
            )
        latest = {}
        for row in existing:
            key = tick_identity(row)
            prev = latest.get(key)
            if prev is None or (row.revision, row.ts_available, row.ts_ingested) >= (
                prev.revision, prev.ts_available, prev.ts_ingested,
            ):
                latest[key] = row
        rows = []
        committed = self._now_ms()
        for event in events:
            published = event.ts_published if isinstance(event, (TradeEvent, ReceivedOrderBookEvent)) else None
            times = dict(
                ts_available=max(event.ts_event, published or 0, event.ts_ingested),
                ts_committed=max(committed, event.ts_ingested),
            )
            if isinstance(event, TradeEvent):
                row = Trade(
                    **event.model_dump(), **times, sequence=event.sequence,
                    packet_id=event.packet_id, packet_index=event.packet_index,
                )
            elif isinstance(event, ReceivedOrderBookEvent):
                row = Quote(
                    **event.model_dump(), **times,
                    connection_id=event.connection_id, update_id=event.update_id,
                )
            else:
                row = Quote(**event.model_dump(), **times)
            key = tick_identity(row)
            prev = latest.get(key)
            if prev is not None:
                if isinstance(key, tuple):  # 同一连接代际的盘口更新只有一个版本。
                    continue
                if row.ts_event == prev.ts_event and tick_payload(row) == tick_payload(prev):
                    continue
                row = replace(row, revision=prev.revision + 1, ts_available=max(row.ts_available, prev.ts_available))
            rows.append(row)
            latest[key] = row
        if rows:
            self._store.publish_ticks(spec, kind, rows)

    def backfill_trades(self, instrument: InstrumentSpec, day: date) -> int:
        """下载线程完整校验文件，写入线程每批再确认只补缺。"""
        if self._archive_reader is None:
            raise ValueError("该市场包未提供逐笔归档读取能力")
        with self._archive_reader.open_trade_archive(instrument, day, self._now_ms) as read_trades:
            scales = tick_column_scales(instrument, TickKind.TRADES)
            for row in read_trades():
                validate_tick(row, instrument, TickKind.TRADES, scales)
            return sum(
                self._write_archive_batch(instrument, batch)
                for _, rows in groupby(read_trades(), key=lambda row: partition_day(row.ts_event))
                for batch in batched(rows, TICK_BATCH_ROWS)
            )

    def _write_archive_batch(self, instrument: InstrumentSpec, rows: Sequence[Trade]) -> int:
        # 归档线程一次只保留一个待重试批次，每次尝试仍由唯一写入线程执行。
        failure = None
        for _ in range(TICK_WRITE_ATTEMPTS):
            try:
                return self._writer.submit(self._publish_archive_batch, instrument, rows).result()
            except Exception as exc:
                failure = exc
        self._log_gap(instrument, TickKind.TRADES, rows, "归档批次重试耗尽")
        assert failure is not None
        raise failure

    def _publish_archive_batch(self, instrument: InstrumentSpec, rows: Sequence[Trade]) -> int:
        existing = {
            row.trade_id for row in self._store.read_trade_revisions_by_id(
                instrument, {row.trade_id.bytes for row in rows},
            )
        }
        committed = self._now_ms()
        missing = []
        for row in rows:
            if row.trade_id not in existing:
                existing.add(row.trade_id)
                missing.append(replace(row, availability="inferred", ts_committed=max(committed, row.ts_ingested)))
        if missing:
            self._store.publish_ticks(instrument, TickKind.TRADES, missing)
        return len(missing)
