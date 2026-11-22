"""冷库衍生品写入，与收盘柱共用写入线程和发布清单。"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence, Set
from concurrent.futures import Future, ThreadPoolExecutor
from itertools import groupby
from time import monotonic

from talaria.foundation.derivatives import (
    DerivativeDataConnector,
    DerivativeKind,
    DerivativePoint,
    derivative_identity,
    latest_visible_derivatives,
)
from talaria.foundation.feed import DerivativeTickEvent, FeedEvent
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.timeframes import timeframe_ms
from talaria.perception.backfill import BackfillPolicy, derivative_history_pages, derivative_series
from talaria.perception.cold_store import ColdStore, partition_month

logger = logging.getLogger(__name__)
type HistorySeries = tuple[tuple[DerivativeKind, str | None], ...]
type HistoryRequest = tuple[str, HistorySeries, int, int | None, int | None, bool]
FLUSH_SECONDS = 1
SETTLEMENT_RETRY_SECONDS = 30
SETTLEMENT_RECONCILE_SECONDS = 60
SETTLEMENT_RECONCILE_LOOKBACK_MS = 86_400_000


class DerivativePublisher:
    def __init__(
        self, store: ColdStore, *, instruments: Sequence[InstrumentSpec],
        connector: DerivativeDataConnector | None, policy: BackfillPolicy, now_ms: Callable[[], int],
        writer: ThreadPoolExecutor, history: ThreadPoolExecutor,
        on_publish: Callable[[str, DerivativeKind, int, int], None] | None,
        funding_intervals_ms: Mapping[str, int],
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
        self._funding_intervals = funding_intervals_ms
        self._funding_subscribed: dict[str, int] = {}
        self._funding_subscription_cutoffs: dict[str, int] = {}
        self._funding_checks: dict[tuple[str, int], tuple[float, Future[bool]] | None] = {}
        self._reconciled_through: dict[str, int] = {}
        self._reconcile_jobs: dict[str, tuple[int, Future[bool]]] = {}
        self._next_reconcile = monotonic() + SETTLEMENT_RECONCILE_SECONDS
        self._pending: dict[HistoryRequest, Future[None]] = {}
        self._lock = threading.Lock()

    def ingest(self, event: FeedEvent) -> None:
        if not isinstance(event, DerivativeTickEvent) or event.instrument_id not in self._instruments:
            return
        self._ticks.append(event)
        instrument = event.instrument_id
        now = self._now_ms()
        previous = self._next_funding.get(instrument)
        scheduled = event.next_funding_ts
        if self._connector is not None and scheduled is not None and scheduled > event.ts_event:
            self._funding_subscribed.setdefault(instrument, event.ts_event)
            cutoff = self._funding_subscription_cutoffs.pop(instrument, None)
            interval = self._funding_intervals.get(instrument)
            if cutoff is not None and interval is not None:
                for boundary in range(scheduled - interval, cutoff, -interval):
                    self._funding_checks.setdefault((instrument, boundary), None)
            if previous is None or scheduled > previous:
                self._next_funding[instrument] = scheduled
                self._funding_checks.setdefault((instrument, scheduled), None)
        self._check_funding(now)

    def track_funding(self, instrument: str, subscribed_ts: int) -> None:
        self._funding_subscribed.setdefault(instrument, subscribed_ts)
        self._funding_subscription_cutoffs.setdefault(instrument, subscribed_ts)

    async def flush_periodically(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            now = self._now_ms()
            self._check_funding(now)
            if monotonic() >= self._next_reconcile:
                self._next_reconcile = monotonic() + SETTLEMENT_RECONCILE_SECONDS
                self._reconcile_funding(now)
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
        series = derivative_series(timeframes)
        return self._request(instrument, (instrument.instrument_id, series, cutoff_ts, None, None, False))

    def request_range(
        self, instrument: InstrumentSpec, kind: DerivativeKind, timeframes: Sequence[str | None],
        *, start: int, end: int, cutoff: int, refresh: bool = False,
    ) -> Future[None] | None:
        if start > min(end, cutoff):
            return None
        series = tuple((kind, tf) for tf in timeframes)
        return self._request(instrument, (instrument.instrument_id, series, cutoff, start, end, refresh))

    def _request(self, instrument: InstrumentSpec, key: HistoryRequest) -> Future[None] | None:
        if self._connector is None:
            return None
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

    def _backfill(self, instrument: InstrumentSpec, key: HistoryRequest) -> None:
        _, series, cutoff, start, end, refresh = key
        try:
            for kind, timeframe in series:
                try:
                    if not refresh and start is not None and end is not None and self._range_covered(
                        instrument, kind, timeframe, start, end, cutoff,
                    ):
                        continue
                    known: Set[int] = frozenset()
                    lower = start
                    if refresh and start is not None:
                        prior = self._store.read_derivatives(
                            instrument, kind, end_ts=start - 1, historical_only=True,
                        )
                        known = {point.ts_event for point in prior}
                        lower = min(known, default=start)
                    self._backfill_series(
                        instrument, kind, timeframe, cutoff, start=lower, end=end,
                        refresh_before=start if refresh else None, known=known,
                    )
                except Exception as exc:
                    if refresh:
                        raise
                    logger.warning("衍生品回填失败（%s %s %s）：%s", instrument.instrument_id, kind, timeframe, exc)
        finally:
            with self._lock:
                self._pending.pop(key, None)

    def _range_covered(
        self, instrument: InstrumentSpec, kind: DerivativeKind, timeframe: str | None,
        start: int, end: int, cutoff: int,
    ) -> bool:
        if timeframe is None:
            interval = self._funding_intervals.get(instrument.instrument_id)
            if interval is None:
                return False
            end = min(end, cutoff)
        else:
            interval = timeframe_ms(timeframe)
            end = min(end, cutoff - interval)
        first = (start + interval - 1) // interval * interval
        if first > end:
            return timeframe is not None
        rows = self._store.read_derivatives(
            instrument, kind, start_ts=start, end_ts=end, as_of=cutoff, historical_only=True,
        )
        covered = {point.ts_event for point in rows if point.timeframe == timeframe}
        return all(ts in covered for ts in range(first, end + 1, interval))

    def _backfill_series(
        self, instrument: InstrumentSpec, kind: DerivativeKind, timeframe: str | None, cutoff: int,
        *, start: int | None = None, end: int | None = None,
        refresh_before: int | None = None, known: Set[int] = frozenset(),
    ) -> None:
        assert self._connector is not None
        if start is not None and timeframe is not None:
            last_closed = cutoff - timeframe_ms(timeframe)
            end = last_closed if end is None else min(end, last_closed)
        pages = derivative_history_pages(
            self._connector, self._policy, symbol=instrument.symbol,
            kind=kind, timeframe=timeframe, cutoff=cutoff, start=start, end=end,
        )
        for page in pages:
            received = self._now_ms()
            points = [
                row.to_point(instrument.instrument_id) for row in page
                if refresh_before is None or row.ts_event >= refresh_before or row.ts_event in known
            ]
            if points:
                self._writer.submit(self._write_points, instrument, kind, points, received=received).result()

    def _check_funding(self, now: int) -> None:
        for key, pending in list(self._funding_checks.items()):
            instrument, boundary = key
            if pending is not None:
                retry_at, future = pending
                if not future.done():
                    continue
                if future.result():
                    del self._funding_checks[key]
                    continue
                if monotonic() < retry_at:
                    continue
            if boundary <= now:
                try:
                    future = self._history.submit(self._confirm_settlement, self._instruments[instrument], boundary)
                except Exception as exc:
                    logger.warning("资金费率结算任务提交失败（%s %s）：%s", instrument, boundary, exc)
                    continue
                self._funding_checks[key] = (monotonic() + SETTLEMENT_RETRY_SECONDS, future)

    def _reconcile_funding(self, now: int) -> None:
        if self._connector is None:
            return
        for instrument, subscribed in self._funding_subscribed.items():
            job = self._reconcile_jobs.get(instrument)
            if job is not None:
                cutoff, future = job
                if not future.done():
                    continue
                if future.result():
                    self._reconciled_through[instrument] = cutoff
                del self._reconcile_jobs[instrument]
            lookback = now - SETTLEMENT_RECONCILE_LOOKBACK_MS
            start = max(0, min(self._reconciled_through.get(instrument, subscribed), lookback))
            try:
                future = self._history.submit(self._reconcile, self._instruments[instrument], start, now)
            except Exception as exc:
                logger.warning("资金费率对账任务提交失败（%s）：%s", instrument, exc)
                continue
            self._reconcile_jobs[instrument] = now, future

    def _reconcile(self, instrument: InstrumentSpec, start: int, cutoff: int) -> bool:
        try:
            self._backfill_series(instrument, DerivativeKind.FUNDING_RATE, None, cutoff, start=start)
        except Exception as exc:
            logger.warning("资金费率对账失败（%s）：%s", instrument.instrument_id, exc)
            return False
        return True

    def _confirm_settlement(self, instrument: InstrumentSpec, boundary: int) -> bool:
        assert self._connector is not None
        try:
            rows = self._connector.fetch_funding_settlements(
                symbol=instrument.symbol, end_ts=boundary, limit=1,
            )
            received = self._now_ms()
            for row in rows:
                point = row.to_point(instrument.instrument_id)
                if row.symbol == instrument.symbol and point.ts_event == boundary and point.is_funding_settlement:
                    point = point.model_copy(update={
                        "ts_available": max(boundary, received),
                        "ts_ingested": received,
                    })
                    self._writer.submit(
                        self._write_points, instrument, DerivativeKind.FUNDING_RATE, [point],
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
                    settled=False if kind is DerivativeKind.FUNDING_RATE else None,
                ))
        for (instrument, kind), points in groups.items():
            try:
                self._write_points(self._instruments[instrument], kind, points)
            except Exception as exc:
                logger.warning("衍生品实时写入失败（%s %s）：%s", instrument, kind, exc)

    def _write_points(
        self, instrument: InstrumentSpec, kind: DerivativeKind, points: Sequence[DerivativePoint], *,
        received: int | None = None,
    ) -> None:
        ordered = sorted(points, key=lambda point: (point.ts_event, derivative_identity(point)))
        for _, group in groupby(ordered, key=lambda point: partition_month(point.ts_event)):
            batch = list(group)
            revisions = self._store.read_derivative_revisions(
                instrument, kind, start_ts=batch[0].ts_event, end_ts=batch[-1].ts_event,
            )
            latest = {derivative_identity(point): point for point in latest_visible_derivatives(revisions)}
            observed_values: dict[tuple, set] = defaultdict(set)
            for revision in revisions:
                if revision.is_funding_settlement:
                    observed_values[derivative_identity(revision)].add(revision.value)
            pending = []
            committed = self._now_ms()
            for point in batch:
                identity = derivative_identity(point)
                previous = latest.get(identity)
                available = point.ts_available
                ingested = point.ts_ingested
                if previous is not None:
                    if point.value == previous.value:
                        # 同一实际费率的历史证据可补上更早的可获取时间。
                        if (not point.is_funding_settlement or available >= previous.ts_available
                                or observed_values[identity] != {point.value}):
                            continue
                    else:
                        ingested = point.ts_ingested if received is None else received
                        available = max(available, ingested, previous.ts_available)
                point = point.model_copy(update={
                    "ts_available": available, "ts_ingested": ingested,
                    "ts_committed": max(committed, ingested, available),
                    "revision": 0 if previous is None else previous.revision + 1,
                })
                latest[identity] = point
                if point.is_funding_settlement:
                    observed_values[identity].add(point.value)
                pending.append(point)
            if pending:
                self._store.publish_derivatives(instrument, kind, pending)
                if self._on_publish is not None:
                    self._on_publish(instrument.instrument_id, kind, pending[0].ts_event, pending[-1].ts_event)
