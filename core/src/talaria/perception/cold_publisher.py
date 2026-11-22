"""常驻服务的冷库写入器，接收实时收盘柱并独立补历史。"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from itertools import groupby

from talaria.foundation.bars import CompactBar
from talaria.foundation.connector import MarketDataConnector, RawBar
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.timeframes import timeframe_ms
from talaria.perception.backfill import BackfillPolicy
from talaria.perception.cold_store import ColdBatch, ColdStore, partition_month

logger = logging.getLogger(__name__)
COMPACTION_INTERVAL_SECONDS = 15 * 60


class ColdPublisher:
    """REST 等待与写盘分别排队，实时监听和图表请求无需等待冷库。"""

    def __init__(
        self,
        store: ColdStore,
        *,
        instruments: Sequence[InstrumentSpec],
        connector: MarketDataConnector | None,
        policy: BackfillPolicy,
        now_ms: Callable[[], int],
    ) -> None:
        self._store = store
        self._instruments = instruments
        self._connector = connector
        self._policy = policy
        self._now_ms = now_ms
        self._writes = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cold-writer")
        self._history = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cold-history")
        self._pending_history: set[tuple[str, str, int, int]] = set()
        self._history_lock = threading.Lock()

    def record_close(
        self, instrument: InstrumentSpec, timeframe: str, bar: CompactBar, received_ts: int
    ) -> None:
        """接收交易所实时交付的已收盘柱。"""
        if bar.close is None:  # FEED_GAP 不归档
            return
        assert all(value is not None for value in (
            bar.open, bar.high, bar.low, bar.volume, bar.turnover,
        ))
        raw = RawBar(
            symbol=instrument.symbol, timeframe=timeframe, ts_event=bar.timestamp,
            open=bar.open, high=bar.high, low=bar.low, close=bar.close,
            volume=bar.volume, turnover=bar.turnover, ts_available=bar.ts_available,
            ts_ingested=received_ts, ts_committed=received_ts, precision=bar.precision,
        )
        self._queue_publish(instrument, timeframe, raw)

    def record_recovered_bar(self, instrument: InstrumentSpec, bar: RawBar) -> None:
        """断线恢复取得的 REST 柱也走只补缺的历史路径。"""
        self._queue_publish(instrument, bar.timeframe, bar, history_only=True)

    def _queue_publish(
        self, instrument: InstrumentSpec, timeframe: str, bar: RawBar, *, history_only: bool = False,
    ) -> None:
        try:
            self._writes.submit(self._publish, instrument, timeframe, [bar], history_only=history_only)
        except Exception as exc:
            logger.warning("冷库发布任务提交失败（%s %s）：%s", instrument.instrument_id, timeframe, exc)

    def backfill_subscription(
        self, instrument: InstrumentSpec, timeframe: str, subscribed_ts: int
    ) -> None:
        interval = timeframe_ms(timeframe)
        self.request_history(
            instrument, timeframe,
            start_ts=subscribed_ts // interval * interval - self._policy.target_bars * interval,
            end_ts=subscribed_ts - interval,
        )

    def request_history(
        self, instrument: InstrumentSpec, timeframe: str, *, start_ts: int, end_ts: int,
    ) -> None:
        if self._connector is None or start_ts > end_ts:
            return
        key = (instrument.instrument_id, timeframe, start_ts, end_ts)
        with self._history_lock:
            if key in self._pending_history:
                return
            self._pending_history.add(key)
            try:
                self._history.submit(self._backfill, key, instrument)
            except Exception as exc:
                self._pending_history.remove(key)
                logger.warning("冷库历史回填任务提交失败（%s %s）：%s", instrument.instrument_id, timeframe, exc)

    async def compact_periodically(self) -> None:
        while True:
            await asyncio.sleep(COMPACTION_INTERVAL_SECONDS)
            try:
                future = self._writes.submit(self._store.compact, self._instruments)
                await asyncio.wrap_future(future)
            except Exception as exc:
                logger.warning("冷库合并失败：%s", exc)

    async def close(self) -> None:
        await asyncio.to_thread(self._history.shutdown)
        await asyncio.to_thread(self._writes.shutdown)

    def _backfill(self, key: tuple[str, str, int, int], instrument: InstrumentSpec) -> None:
        _, timeframe, start, end = key
        try:
            assert self._connector is not None
            interval = timeframe_ms(timeframe)
            start = max(0, (start + interval - 1) // interval * interval)
            cursor = end // interval * interval
            covered = {
                bar.ts_event
                for bar in self._store.read_bars(
                    instrument, timeframe, start_ts=start, end_ts=end,
                )
            }
            for _ in range(self._policy.max_pages):
                while cursor in covered:
                    cursor -= interval
                if cursor < start:
                    break
                limit = min(self._policy.page_limit, (cursor - start) // interval + 1)
                page = self._connector.fetch_bars(
                    symbol=instrument.symbol, timeframe=timeframe, end_ts=cursor, limit=limit,
                )
                received_ts = self._now_ms()
                eligible = [bar for bar in page if start <= bar.ts_event <= cursor]
                if not eligible:
                    break
                missing = [
                    replace(bar, ts_ingested=received_ts) for bar in eligible
                    if bar.ts_event not in covered
                ]
                if missing:
                    self._writes.submit(
                        self._publish, instrument, timeframe, missing, history_only=True,
                    ).result()
                cursor = min(bar.ts_event for bar in eligible) - interval
        except Exception as exc:
            logger.warning("冷库历史回填失败（%s %s）：%s", instrument.instrument_id, timeframe, exc)
        finally:
            with self._history_lock:
                self._pending_history.discard(key)

    def _publish(
        self, instrument: InstrumentSpec, timeframe: str, candidates: Sequence[RawBar],
        *, history_only: bool = False,
    ) -> None:
        try:
            if history_only:
                interval = timeframe_ms(timeframe)
                candidates = [
                    replace(bar, ts_available=bar.ts_event + interval) for bar in candidates
                ]
            ordered = sorted(candidates, key=lambda bar: bar.ts_event)
            for _, group in groupby(ordered, key=lambda bar: partition_month(bar.ts_event)):
                bars = list(group)
                latest = {
                    bar.ts_event: bar
                    for bar in self._store.read_bars(
                        instrument, timeframe, start_ts=bars[0].ts_event, end_ts=bars[-1].ts_event,
                    )
                }
                rows = []
                committed = self._now_ms()
                for raw in bars:
                    previous = latest.get(raw.ts_event)
                    if previous is not None:
                        if history_only or replace(
                            previous, ts_available=raw.ts_available, ts_ingested=raw.ts_ingested,
                            ts_committed=raw.ts_committed, revision=raw.revision,
                        ) == raw:
                            continue
                    bar = replace(
                        raw,
                        ts_committed=max(committed, raw.ts_ingested),
                        revision=0 if previous is None else previous.revision + 1,
                    )
                    rows.append(bar)
                    latest[bar.ts_event] = bar
                if rows:
                    self._store.publish(ColdBatch(instrument, timeframe, rows))
        except Exception as exc:
            logger.warning("冷库发布失败（%s %s）：%s", instrument.instrument_id, timeframe, exc)
