"""常驻服务的冷库写入器，接收实时收盘柱并独立补历史。"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from itertools import groupby

from talaria.foundation.bars import CompactBar
from talaria.foundation.connector import MarketDataConnector, RawBar
from talaria.foundation.errors import FeedUnavailableError
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.timeframes import timeframe_ms
from talaria.perception.backfill import BackfillPolicy
from talaria.perception.bar_range import range_visible_bars, unpublished_revisions
from talaria.perception.cold_store import ColdBatch, ColdStore, partition_month
from talaria.perception.hot_buffer import HotSlot, HotStreamBuffer

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
        hot_buffer: HotStreamBuffer | None = None,
        has_channel: Callable[[str, str], bool] | None = None,
        on_publish: Callable[[tuple[str, str], int, int], None] | None = None,
        on_history_complete: Callable[[str, str], None] | None = None,
    ) -> None:
        self._store = store
        self._instruments = instruments
        self._connector = connector
        self._policy = policy
        self._now_ms = now_ms
        self._hot_buffer = hot_buffer
        self._has_channel = has_channel
        self._on_publish = on_publish
        self._on_history_complete = on_history_complete
        self._writes = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cold-writer")
        self._history = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cold-history")
        self._pending_history: dict[tuple[str, str, int, int, frozenset[int], int | None], Future[None]] = {}
        self._history_lock = threading.Lock()

    def record_close(
        self, instrument: InstrumentSpec, timeframe: str, bar: CompactBar, received_ts: int,
        revision: int = 0,
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
            revision=revision,
        )
        try:
            slot = self._hot_buffer.slot((instrument.instrument_id, timeframe), raw.ts_event) if self._hot_buffer else None
            self._writes.submit(self._publish_close, instrument, raw, slot)
        except Exception as exc:
            logger.warning("冷库发布任务提交失败（%s %s）：%s", instrument.instrument_id, timeframe, exc)

    def _publish_close(self, instrument: InstrumentSpec, raw: RawBar, slot: HotSlot | None) -> None:
        try:
            cold = self._store.read_revisions(
                instrument, raw.timeframe, start_ts=raw.ts_event, end_ts=raw.ts_event,
            )
            # 重发可以补一次失败的写入，仍使用原先占用的编号。
            prior = slot.revisions() if slot is not None else []
            earlier = [bar for bar in prior if bar.revision < raw.revision]
            version = unpublished_revisions(cold, [*earlier, raw]).get(raw.revision)
            if version is not None:
                bar = replace(version, ts_committed=max(self._now_ms(), raw.ts_ingested))
                self._publish(instrument, raw.timeframe, [bar])
        except Exception as exc:
            logger.warning("冷库发布失败（%s %s）：%s", instrument.instrument_id, raw.timeframe, exc)

    def record_recovered_bar(self, instrument: InstrumentSpec, bar: RawBar) -> None:
        """断线恢复取得的 REST 柱也走只补缺的历史路径。"""
        try:
            self._writes.submit(self._publish_history, instrument, bar.timeframe, [bar])
        except Exception as exc:
            logger.warning("冷库发布任务提交失败（%s %s）：%s", instrument.instrument_id, bar.timeframe, exc)

    def backfill_subscription(
        self, instrument: InstrumentSpec, timeframe: str, subscribed_ts: int
    ) -> None:
        interval = timeframe_ms(timeframe)
        self.request_history(
            instrument, timeframe,
            start_ts=subscribed_ts // interval * interval - self._policy.target_bars * interval,
            end_ts=subscribed_ts - interval,
            max_pages=self._policy.max_pages,
        )

    def request_history(
        self, instrument: InstrumentSpec, timeframe: str, *, start_ts: int, end_ts: int,
        hot_timestamps: frozenset[int] = frozenset(), max_pages: int | None = None,
    ) -> Future[None] | None:
        if self._connector is None or start_ts > end_ts:
            return
        key = (instrument.instrument_id, timeframe, start_ts, end_ts, hot_timestamps, max_pages)
        with self._history_lock:
            if key in self._pending_history:
                return self._pending_history[key]
            try:
                future = self._history.submit(self._backfill, key, instrument)
            except Exception as exc:
                logger.warning("冷库历史回填任务提交失败（%s %s）：%s", instrument.instrument_id, timeframe, exc)
                return None
            self._pending_history[key] = future
            return future

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

    def _backfill(
        self, key: tuple[str, str, int, int, frozenset[int], int | None], instrument: InstrumentSpec,
    ) -> None:
        _, timeframe, start, end, hot_timestamps, max_pages = key
        try:
            assert self._connector is not None
            interval = timeframe_ms(timeframe)
            start = max(0, (start + interval - 1) // interval * interval)
            cursor = end // interval * interval
            covered = hot_timestamps.union(
                bar.ts_event
                for bar in self._store.read_bars(
                    instrument, timeframe, start_ts=start, end_ts=end,
                )
            )
            pages = 0
            while max_pages is None or pages < max_pages:
                while cursor in covered:
                    cursor -= interval
                if cursor < start:
                    break
                limit = min(self._policy.page_limit, (cursor - start) // interval + 1)
                page = self._connector.fetch_bars(
                    symbol=instrument.symbol, timeframe=timeframe, end_ts=cursor, limit=limit,
                )
                pages += 1
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
                        self._publish_history, instrument, timeframe, missing, check_hot=True,
                    ).result()
                cursor = min(bar.ts_event for bar in eligible) - interval
        except Exception as exc:
            logger.warning("冷库历史回填失败（%s %s）：%s", instrument.instrument_id, timeframe, exc)
        finally:
            with self._history_lock:
                self._pending_history.pop(key, None)
            if self._on_history_complete is not None:
                self._on_history_complete(instrument.instrument_id, timeframe)

    def _publish_history(
        self, instrument: InstrumentSpec, timeframe: str, candidates: Sequence[RawBar],
        *, check_hot: bool = False,
    ) -> None:
        try:
            interval = timeframe_ms(timeframe)
            ordered = sorted(candidates, key=lambda bar: bar.ts_event)
            for _, group in groupby(ordered, key=lambda bar: partition_month(bar.ts_event)):
                bars = list(group)
                covered = {
                    bar.ts_event
                    for bar in self._store.read_bars(
                        instrument, timeframe, start_ts=bars[0].ts_event, end_ts=bars[-1].ts_event,
                    )
                }
                if check_hot and self._hot_buffer is not None:
                    active = self._has_channel is None or self._has_channel(
                        instrument.instrument_id, timeframe,
                    )
                    hot = self._hot_buffer.revisions(
                        instrument_id=instrument.instrument_id, timeframe=timeframe,
                        start_ts=bars[0].ts_event, end_ts=bars[-1].ts_event,
                    )
                    covered.update(
                        bar.ts_event for bar in hot if active or (bar.confirmed and not bar.synthetic)
                    )
                rows = []
                committed = self._now_ms()
                for bar in bars:
                    if bar.ts_event not in covered:
                        rows.append(replace(
                            bar, ts_available=bar.ts_event + interval,
                            ts_committed=max(committed, bar.ts_ingested),
                        ))
                        covered.add(bar.ts_event)
                if rows:
                    self._publish(instrument, timeframe, rows)
        except Exception as exc:
            logger.warning("冷库发布失败（%s %s）：%s", instrument.instrument_id, timeframe, exc)

    def _publish(self, instrument: InstrumentSpec, timeframe: str, rows: Sequence[RawBar]) -> None:
        self._store.publish(ColdBatch(instrument, timeframe, rows))
        if self._on_publish is not None:
            self._on_publish((instrument.instrument_id, timeframe), rows[0].ts_event, rows[-1].ts_event)

    def refresh_history(
        self, instrument: InstrumentSpec, timeframe: str, expected: dict[int, RawBar | None],
    ) -> Future[None]:
        """重同步既核对已有槽的交易所修订，也补断线期间应已收盘的槽。"""
        return self._history.submit(self._refresh_history, instrument, timeframe, expected)

    def _refresh_history(
        self, instrument: InstrumentSpec, timeframe: str, expected: dict[int, RawBar | None],
    ) -> None:
        if self._connector is None:
            raise FeedUnavailableError("历史来源不可用，请重试")
        interval = timeframe_ms(timeframe)
        slots = sorted(expected)
        # 只查询实际需要的连续区间，每页至多 page_limit 个时间槽。
        for _, group in groupby(enumerate(slots), key=lambda item: item[1] - item[0] * interval):
            run = [ts for _, ts in group]
            for offset in range(0, len(run), self._policy.page_limit):
                page = run[offset:offset + self._policy.page_limit]
                try:
                    fetched = self._connector.fetch_bars(
                        symbol=instrument.symbol, timeframe=timeframe, end_ts=page[-1], limit=len(page),
                    )
                except Exception as exc:
                    raise FeedUnavailableError("交易所范围重取失败，请重试") from exc
                received_ts = self._now_ms()
                rows = {bar.ts_event: bar for bar in fetched if bar.confirmed and not bar.synthetic}
                if not rows.keys() >= set(page):
                    raise FeedUnavailableError("交易所范围重取缺少已确认柱，请重试")
                try:
                    self._writes.submit(
                        self._publish_refresh, instrument, timeframe, [rows[ts] for ts in page], expected, received_ts,
                    ).result()
                except Exception as exc:
                    raise FeedUnavailableError("重同步修订发布失败，请重试") from exc

    def _publish_refresh(
        self, instrument: InstrumentSpec, timeframe: str,
        page: Sequence[RawBar], expected: dict[int, RawBar | None], received_ts: int,
    ) -> None:
        start, end = page[0].ts_event, page[-1].ts_event
        cold = self._store.read_revisions(instrument, timeframe, start_ts=start, end_ts=end)
        hot = [] if self._hot_buffer is None else self._hot_buffer.revisions(
            instrument_id=instrument.instrument_id, timeframe=timeframe, start_ts=start, end_ts=end,
        )
        now = self._now_ms()
        current = {bar.ts_event: bar for bar in range_visible_bars(cold, hot, as_of=now, include_open=True)}
        archived = {ts: list(group) for ts, group in groupby(
            sorted(cold, key=lambda b: b.ts_event), key=lambda b: b.ts_event,
        )}
        versions = {ts: list(group) for ts, group in groupby(hot, key=lambda b: b.ts_event)}
        active = self._has_channel is None or self._has_channel(instrument.instrument_id, timeframe)
        changed = []
        for bar in page:
            ts = bar.ts_event
            latest = current.get(ts)
            if latest is not None and not latest.confirmed:
                if active and not latest.synthetic:
                    continue  # 当前实时预览受保护，由调用方检查确认覆盖。
                latest = None
            # REST 请求期间已收到新的确认，保留较新的实际观察。
            before = expected[ts]
            if latest is not None and (before is None or latest.revision != before.revision):
                continue
            prior = versions.get(ts, [])
            revision = prior[-1].revision + 1 if prior else 0
            candidate = replace(
                bar, revision=revision, ts_available=ts + timeframe_ms(timeframe),
                ts_ingested=received_ts, ts_committed=max(received_ts, now),
            )
            pending = unpublished_revisions(archived.get(ts, []), [*prior, candidate])
            if revision in pending:
                # 同一批次落下先前写盘失败的热版本，保留修订编号锚点。
                changed.extend(replace(row, ts_committed=max(now, row.ts_ingested)) for row in pending.values())
        for _, month in groupby(changed, key=lambda b: partition_month(b.ts_event)):
            self._publish(instrument, timeframe, list(month))
