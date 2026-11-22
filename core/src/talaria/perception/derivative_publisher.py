"""冷库衍生品写入，与收盘柱共用写入线程和发布清单。"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from itertools import groupby
from time import monotonic

from talaria.foundation.derivatives import (
    OI_TIMEFRAME_BY_TIMEFRAME,
    DerivativeDataConnector,
    DerivativeKind,
    DerivativePoint,
    derivative_identity,
)
from talaria.foundation.feed import DerivativeTickEvent, FeedEvent
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.timeframes import timeframe_ms
from talaria.perception.backfill import BackfillPolicy
from talaria.perception.cold_store import ColdStore, partition_month

logger = logging.getLogger(__name__)
FLUSH_SECONDS = 1
SETTLEMENT_RETRY_SECONDS = 30


class DerivativePublisher:
    def __init__(
        self, store: ColdStore, *, instruments: Sequence[InstrumentSpec],
        connector: DerivativeDataConnector | None, policy: BackfillPolicy, now_ms: Callable[[], int],
        writer: ThreadPoolExecutor, history: ThreadPoolExecutor,
        on_publish: Callable[[str, DerivativeKind, int, int], None] | None,
    ) -> None:
        self._store = store
        self._instruments = {spec.instrument_id: spec for spec in instruments}
        self._connector = connector
        self._policy = policy
        self._now_ms = now_ms
        self._writer = writer
        self._history = history
        self._on_publish = on_publish
        self._ticks: list[DerivativeTickEvent] = []
        self._next_funding: dict[str, int] = {}
        self._checked_funding: dict[str, int] = {}
        self._funding_checks: dict[str, tuple[int, float, Future[bool]]] = {}
        self._pending: dict[tuple[str, tuple[str, ...], int], Future[None]] = {}
        self._lock = threading.Lock()

    def ingest(self, event: FeedEvent) -> None:
        if not isinstance(event, DerivativeTickEvent) or event.instrument_id not in self._instruments:
            return
        self._ticks.append(event)
        instrument = event.instrument_id
        self._check_funding(instrument, max(event.ts_event, self._now_ms()))
        scheduled = event.next_funding_ts
        if scheduled is not None and scheduled > event.ts_event:
            self._next_funding[instrument] = max(scheduled, self._next_funding.get(instrument, 0))

    async def flush_periodically(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            for instrument in self._next_funding:
                self._check_funding(instrument, self._now_ms())
            await self.flush()

    async def flush(self) -> None:
        if self._ticks:
            ticks, self._ticks = self._ticks, []
            # Cancellation of the periodic task must not cancel an already queued write.
            try:
                await asyncio.shield(asyncio.wrap_future(self._writer.submit(self._write_ticks, ticks)))
            except Exception as exc:
                logger.warning("衍生品实时写入批次失败：%s", exc)

    def request_history(
        self, instrument: InstrumentSpec, timeframes: Sequence[str], *, cutoff_ts: int,
    ) -> Future[None] | None:
        if self._connector is None:
            return None
        key = instrument.instrument_id, tuple(sorted(set(timeframes))), cutoff_ts
        with self._lock:
            if key in self._pending:
                return self._pending[key]
            try:
                future = self._history.submit(self._backfill, instrument, key)
            except Exception as exc:
                logger.warning("衍生品回填任务提交失败（%s）：%s", instrument.instrument_id, exc)
                return None
            self._pending[key] = future
            return future

    def _backfill(self, instrument: InstrumentSpec, key: tuple[str, tuple[str, ...], int]) -> None:
        _, timeframes, cutoff = key
        series = [
            *((DerivativeKind.MARK_PRICE, tf) for tf in timeframes),
            (DerivativeKind.FUNDING_RATE, None),
            *((DerivativeKind.OPEN_INTEREST, tf) for tf in sorted({
                OI_TIMEFRAME_BY_TIMEFRAME[tf] for tf in timeframes if tf in OI_TIMEFRAME_BY_TIMEFRAME
            })),
        ]
        try:
            for kind, timeframe in series:
                try:
                    self._backfill_series(instrument, kind, timeframe, cutoff)
                except Exception as exc:
                    logger.warning("衍生品回填失败（%s %s %s）：%s", instrument.instrument_id, kind, timeframe, exc)
        finally:
            with self._lock:
                self._pending.pop(key, None)

    def _backfill_series(
        self, instrument: InstrumentSpec, kind: DerivativeKind, timeframe: str | None, cutoff: int,
    ) -> None:
        assert self._connector is not None
        limit = min(self._policy.page_limit, 1000 if kind is DerivativeKind.MARK_PRICE else 200)
        target = self._policy.target_bars
        cursor = cutoff
        if timeframe is None:
            target = max(8, min(1000, target // 8))
        else:
            interval = timeframe_ms(timeframe)
            cursor = cutoff - interval
            # 连续历史已覆盖请求时无需再次访问 REST。
            end_slot = cursor // interval * interval
            start_slot = max(0, end_slot - (target - 1) * interval)
            rows = self._store.read_derivatives(
                instrument, kind, start_ts=start_slot, end_ts=end_slot, as_of=cutoff, historical_only=True,
            )
            covered = {point.ts_event for point in rows if point.timeframe == timeframe}
            if end_slot >= 0 and covered.issuperset(range(start_slot, end_slot + 1, interval)):
                return

        collected = 0
        for _ in range(self._policy.max_pages):
            if cursor < 0:
                break
            if kind is DerivativeKind.MARK_PRICE:
                assert timeframe is not None
                page = self._connector.fetch_mark_price_bars(
                    symbol=instrument.symbol, timeframe=timeframe, end_ts=cursor, limit=limit,
                )
            elif kind is DerivativeKind.OPEN_INTEREST:
                assert timeframe is not None
                page = self._connector.fetch_open_interest(
                    symbol=instrument.symbol, timeframe=timeframe, end_ts=cursor, limit=limit,
                )
            else:
                page = self._connector.fetch_funding_settlements(
                    symbol=instrument.symbol, end_ts=cursor, limit=limit,
                )
            page = [row for row in page if row.kind is kind and row.timeframe == timeframe
                    and row.symbol == instrument.symbol and row.ts_event <= cursor]
            if not page:
                break
            points = [row.to_point(instrument.instrument_id) for row in page if row.ts_available <= cutoff]
            if points:
                self._writer.submit(self._write_points, instrument, kind, points, history=True).result()
            collected += len(points)
            if len(page) < limit or collected >= target:
                break
            cursor = min(row.ts_event for row in page) - 1

    def _check_funding(self, instrument: str, now: int) -> None:
        if self._connector is None:
            return
        boundary = self._next_funding.get(instrument, 0)
        pending = self._funding_checks.get(instrument)
        if pending is not None:
            previous, retry_at, future = pending
            if not future.done():
                return
            if future.result():
                self._checked_funding[instrument] = previous
                self._funding_checks.pop(instrument)
            elif monotonic() < retry_at:
                return
            elif boundary > now:
                boundary = previous
        if self._checked_funding.get(instrument, 0) < boundary <= now:
            try:
                future = self._history.submit(self._confirm_settlement, self._instruments[instrument], boundary)
            except Exception as exc:
                logger.warning("资金费率结算任务提交失败（%s %s）：%s", instrument, boundary, exc)
                return
            self._funding_checks[instrument] = (boundary, monotonic() + SETTLEMENT_RETRY_SECONDS, future)

    def _confirm_settlement(self, instrument: InstrumentSpec, boundary: int) -> bool:
        assert self._connector is not None
        try:
            rows = self._connector.fetch_funding_settlements(
                symbol=instrument.symbol, end_ts=boundary, limit=1,
            )
            received = self._now_ms()
            for row in rows:
                point = row.to_point(instrument.instrument_id)
                if point.ts_event == boundary and point.is_funding_settlement:
                    point = point.model_copy(update={
                        "ts_available": max(point.ts_available, received),
                        "ts_ingested": received,
                    })
                    self._writer.submit(
                        self._write_points, instrument, DerivativeKind.FUNDING_RATE, [point], history=True,
                    ).result()
                    return True
        except Exception as exc:
            logger.warning("资金费率结算读取失败（%s %s）：%s", instrument.instrument_id, boundary, exc)
        return False

    def _write_ticks(self, ticks: Sequence[DerivativeTickEvent]) -> None:
        groups: dict[tuple[str, DerivativeKind], list[DerivativePoint]] = defaultdict(list)
        for tick in ticks:
            for kind in DerivativeKind:
                value = getattr(tick, kind.value)
                if value is None:
                    continue
                groups[tick.instrument_id, kind].append(DerivativePoint(
                    instrument_id=tick.instrument_id, kind=kind, value=value, ts_event=tick.ts_event,
                    ts_available=max(tick.ts_event, tick.ts_ingested),
                    ts_ingested=tick.ts_ingested, ts_committed=tick.ts_ingested,
                ))
        for (instrument, kind), points in groups.items():
            try:
                self._write_points(self._instruments[instrument], kind, points, history=False)
            except Exception as exc:
                logger.warning("衍生品实时写入失败（%s %s）：%s", instrument, kind, exc)

    def _write_points(
        self, instrument: InstrumentSpec, kind: DerivativeKind, points: Sequence[DerivativePoint], *, history: bool,
    ) -> None:
        ordered = sorted(points, key=lambda point: (point.ts_event, derivative_identity(point)))
        for _, group in groupby(ordered, key=lambda point: partition_month(point.ts_event)):
            batch = list(group)
            current = self._store.read_derivatives(
                instrument, kind, start_ts=batch[0].ts_event, end_ts=batch[-1].ts_event,
            )
            latest = {derivative_identity(point): point for point in current}
            pending = []
            committed = self._now_ms()
            for point in batch:
                identity = derivative_identity(point)
                previous = latest.get(identity)
                if previous is not None and (history or point.value == previous.value):
                    continue
                available = point.ts_available if previous is None else max(
                    point.ts_available, previous.ts_available,
                )
                point = point.model_copy(update={
                    "ts_available": available,
                    "ts_committed": max(committed, point.ts_ingested, available),
                    "revision": 0 if previous is None else previous.revision + 1,
                })
                latest[identity] = point
                pending.append(point)
            if pending:
                self._store.publish_derivatives(instrument, kind, pending)
                if self._on_publish is not None:
                    self._on_publish(instrument.instrument_id, kind, pending[0].ts_event, pending[-1].ts_event)
