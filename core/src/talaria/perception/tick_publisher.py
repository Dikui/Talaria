"""逐笔与买一卖一批量发布到常驻服务的冷库写入线程。"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from itertools import batched
from tempfile import TemporaryFile
from typing import BinaryIO

from talaria.foundation.feed import FeedEvent, OrderBookEvent, TradeEvent
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.ticks import Quote, TickKind, Trade, tick_identity, tick_payload
from talaria.packs.crypto_perp import trade_archive
from talaria.perception.cold_store import (
    TICK_BATCH_ROWS, ColdStore, partition_day, tick_column_scales, validate_tick_values,
)

logger = logging.getLogger(__name__)
TICK_FLUSH_SECONDS = 60


class TickPublisher:
    def __init__(
        self, store: ColdStore, instruments: Sequence[InstrumentSpec],
        now_ms: Callable[[], int], writer: ThreadPoolExecutor,
    ) -> None:
        self._store = store
        self._instruments = {spec.instrument_id: spec for spec in instruments}
        self._now_ms = now_ms
        self._writer = writer
        self._pending: dict[tuple[str, TickKind, str], list[TradeEvent | OrderBookEvent]] = defaultdict(list)

    def ingest(self, event: FeedEvent) -> None:
        if isinstance(event, (TradeEvent, OrderBookEvent)) and event.instrument_id in self._instruments:
            kind = TickKind.TRADES if isinstance(event, TradeEvent) else TickKind.QUOTES
            self._pending[event.instrument_id, kind, partition_day(event.ts_event)].append(event)

    async def flush_periodically(self) -> None:
        while True:
            await asyncio.sleep(TICK_FLUSH_SECONDS)
            await self.flush()

    async def flush(self) -> None:
        if not self._pending:
            return
        pending, self._pending = self._pending, defaultdict(list)
        # 停止定时任务时，已提交的写入仍由冷库写入线程完成。
        futures = []
        for (instrument, kind, _), events in pending.items():
            for batch in batched(events, TICK_BATCH_ROWS):
                futures.append(asyncio.wrap_future(self._writer.submit(
                    self._publish, self._instruments[instrument], kind, batch,
                )))
        await asyncio.shield(asyncio.gather(*futures))

    def _publish(
        self, spec: InstrumentSpec, kind: TickKind, events: Sequence[TradeEvent | OrderBookEvent],
    ) -> None:
        try:
            existing = self._store.read_tick_revisions(
                spec, kind, start_ts=min(event.ts_event for event in events),
                end_ts=max(event.ts_event for event in events),
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
                record = Trade if isinstance(event, TradeEvent) else Quote
                row = record(
                    **event.model_dump(),
                    ts_available=max(event.ts_event, event.ts_ingested),
                    ts_committed=max(committed, event.ts_ingested),
                )
                key = tick_identity(row)
                prev = latest.get(key)
                if prev is not None:
                    if tick_payload(row) == tick_payload(prev):
                        continue
                    row = replace(row, revision=prev.revision + 1, ts_available=max(row.ts_available, prev.ts_available))
                rows.append(row)
                latest[key] = row
            if rows:
                self._store.publish_ticks(spec, kind, rows)
        except Exception:
            logger.warning("实时行情冷库批次写入失败（%s %s）", spec.instrument_id, kind, exc_info=True)

    def backfill_trades(
        self, instrument: InstrumentSpec, day: date, download: Callable[[str, BinaryIO], None] | None,
    ) -> int:
        """下载线程完整校验文件，写入线程每批再确认只补缺。"""
        if instrument.venue != "bybit" or instrument.asset_class != "crypto_perp":
            raise ValueError("逐笔归档只支持 Bybit 永续")
        trade_archive.check_archive_day(day, self._now_ms())
        download = download or trade_archive.download_bybit_archive
        start = trade_archive.archive_day_start(day)
        end = start + trade_archive.DAY_MS - 1
        with TemporaryFile() as source:
            download(trade_archive.archive_url(instrument.symbol, day), source)
            source.seek(0)
            received = self._now_ms()
            scales = tick_column_scales(instrument, TickKind.TRADES)
            count = 0
            for row in trade_archive.iter_archive_trades(source, instrument, day, received):
                validate_tick_values(row, scales)
                count += 1
            if count == 0:
                return 0

            source.seek(0)
            existing = self._writer.submit(
                self._store.read_trade_ids, instrument, start_ts=start, end_ts=end,
            ).result()

            def missing():
                for trade in trade_archive.iter_archive_trades(source, instrument, day, received):
                    trade_id = trade.trade_id.bytes
                    if trade_id not in existing:
                        existing.add(trade_id)
                        yield trade

            return sum(
                self._writer.submit(self._publish_archive_batch, instrument, batch).result()
                for batch in batched(missing(), TICK_BATCH_ROWS)
            )

    def _publish_archive_batch(self, instrument: InstrumentSpec, rows: Sequence[Trade]) -> int:
        start = min(row.ts_event for row in rows)
        end = max(row.ts_event for row in rows)
        existing = self._store.read_trade_ids(instrument, start_ts=start, end_ts=end)
        committed = self._now_ms()
        missing = [
            replace(row, ts_committed=max(committed, row.ts_ingested))
            for row in rows if row.trade_id.bytes not in existing
        ]
        if missing:
            self._store.publish_ticks(instrument, TickKind.TRADES, missing)
        return len(missing)
