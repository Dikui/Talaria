"""快照编排：确保历史 → 合并热端切片 → 按 cutoff 紧凑化 → 原子写 Parquet → 登记清单。"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Callable, Sequence

from talaria.foundation.bars import BarStatus, CompactBar, classify_flat_bar
from talaria.foundation.connector import MarketDataConnector, RawBar
from talaria.foundation.derivatives import (
    OI_TIMEFRAME_BY_TIMEFRAME,
    DerivativeDataConnector,
    DerivativeKind,
    DerivativePoint,
)
from talaria.foundation.errors import ColdStoreError, FeatureDisabledError, FeedUnavailableError
from talaria.foundation.feed import KlineEvent, ReceivedKlineEvent
from talaria.perception.snapshot_events import SnapshotEvent
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.snapshot import SnapshotMeta, SnapshotRequest
from talaria.perception.backfill import (
    BackfillPolicy, derivative_history_pages, derivative_series, ensure_history,
)
from talaria.perception.cold_publisher import ColdPublisher
from talaria.perception.cold_store import ColdReader
from talaria.perception.compaction import compact_series
from talaria.perception.hot_buffer import HotBarProvider
from talaria.perception.raw_store import RawStore, latest_visible_bars
from talaria.perception.snapshot_store import (
    SnapshotBar,
    SnapshotStore,
    write_derivative_parquet,
    write_snapshot_parquet,
)

logger = logging.getLogger(__name__)


def _snapshot_entries(
    bars: Sequence[CompactBar],
    raw_bars: Sequence[RawBar],
    *,
    cutoff_ts: int,
) -> list[SnapshotBar]:
    """为紧凑序列补齐 Committed Time；缺口柱沿用其后真实柱的提交时刻。"""

    committed_by_ts = {bar.ts_event: bar.ts_committed for bar in raw_bars}
    entries: list[SnapshotBar] = []
    following_committed = cutoff_ts
    for bar in reversed(bars):
        committed = committed_by_ts.get(bar.timestamp)
        if committed is not None:
            following_committed = committed
        entries.append(SnapshotBar(bar=bar, ts_committed=following_committed))
    entries.reverse()
    return entries


def _received_kline(instrument_id: str, row: RawBar) -> ReceivedKlineEvent:
    status = classify_flat_bar(open=row.open, high=row.high, low=row.low, close=row.close, volume=row.volume)
    return ReceivedKlineEvent(
        instrument_id=instrument_id, timeframe=row.timeframe, bar=row.to_compact(status=status),
        confirmed=row.confirmed, revision=row.revision, ts_ingested=row.ts_ingested,
    )


class SnapshotBuilder:
    def __init__(
        self,
        *,
        data_dir: Path,
        connector: MarketDataConnector | None,
        raw_store: RawStore,
        snapshot_store: SnapshotStore,
        policy: BackfillPolicy,
        now_ms: Callable[[], int],
        hot_bars: HotBarProvider | None = None,
        on_derivatives: Callable[[str, Sequence[DerivativePoint]], None] | None = None,
        cold_publisher: ColdPublisher | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._connector = connector
        self._raw_store = raw_store
        self._snapshot_store = snapshot_store
        self._policy = policy
        self._now_ms = now_ms
        self._hot_bars = hot_bars
        self._on_derivatives = on_derivatives
        self._cold_publisher = cold_publisher
        self._cold_reader = ColdReader(data_dir)

    def build(
        self,
        *,
        instrument: InstrumentSpec,
        request: SnapshotRequest,
    ) -> SnapshotMeta:
        now = self._now_ms()
        cutoff = request.cutoff_ts if request.cutoff_ts is not None else now
        frames: list[tuple[str, Sequence[SnapshotBar]]] = []

        replay: dict[str, list[SnapshotEvent]] = {}
        read_events = getattr(self._hot_bars, "recent_events", None)
        received = read_events(instrument.instrument_id) if read_events else []
        for timeframe in request.timeframes:
            try:
                cold = self._cold_reader.read_revisions(instrument, timeframe, end_ts=cutoff)
            except ColdStoreError as exc:
                logger.warning("读取冷库快照输入失败，继续使用常规数据源：%s", exc)
                cold = []
            if self._connector is not None:
                try:
                    ensure_history(
                        raw_store=self._raw_store,
                        connector=self._connector,
                        symbol=instrument.symbol,
                        timeframe=timeframe,
                        cutoff_ts=cutoff,
                        policy=self._policy,
                        cold_bars=cold,
                    )
                except FeedUnavailableError as exc:
                    if not cold:
                        raise
                    logger.warning("历史回填失败，继续使用本地快照输入：%s", exc)
            hot = (
                self._hot_bars.recent_bars(
                    instrument_id=instrument.instrument_id, timeframe=timeframe
                )
                if self._hot_bars is not None
                else ()
            )
            revisions = [*cold, *self._raw_store.revisions(instrument.symbol, timeframe), *hot]
            # 已过预计收盘时刻的进行中柱也必须等到确认后才能冻结。
            raw_bars = latest_visible_bars(
                (bar for bar in revisions if bar.confirmed), as_of=cutoff
            )
            if self._connector is None and not raw_bars:
                raise FeatureDisabledError(
                    f"行情采集已在 service.json 中关闭，缺少本地历史："
                    f"{instrument.instrument_id} {timeframe}（截至 {cutoff}）"
                )
            compact = compact_series(raw_bars, timeframe=timeframe, cutoff_ts=cutoff)
            entries = _snapshot_entries(compact, raw_bars, cutoff_ts=cutoff)
            frames.append((timeframe, entries))
            observed = [row for row in received
                        if row.received_at <= cutoff and getattr(row.event, "timeframe", timeframe) == timeframe]
            live_keys = {(row.event.bar.timestamp, row.event.revision) for row in observed if isinstance(row.event, KlineEvent)}
            history = {}
            for row in revisions:
                if (row.ts_event, row.revision) in live_keys or max(row.ts_available, row.ts_ingested) > cutoff:
                    continue
                history[row.ts_event, row.revision, row.ts_available] = _received_kline(instrument.instrument_id, row)
            replay[timeframe] = [SnapshotEvent(0, event) for event in history.values()]
            observed_slots = {slot for slot, _ in live_keys}
            for bar in compact:
                if bar.status is BarStatus.FEED_GAP and bar.timestamp not in observed_slots:
                    replay[timeframe].append(SnapshotEvent(0, KlineEvent(instrument_id=instrument.instrument_id,
                        timeframe=timeframe, bar=bar, confirmed=True, revision=0)))
            replay[timeframe].extend(observed)

        derivative_points = self._derivative_points(
            instrument=instrument, request=request, cutoff_ts=cutoff
        )
        meta = self._freeze(
            instrument, request, frames, cutoff, now, derivative_points=derivative_points, replay=replay
        )
        if derivative_points and self._on_derivatives is not None:
            self._on_derivatives(instrument.instrument_id, derivative_points)
        return meta

    def chart_updates(
        self, instrument: InstrumentSpec, timeframe: str, start_ts: int, after: int,
    ) -> list[KlineEvent]:
        """重启或延迟保存时，补入图表冻结后已经收到的确认与修订。"""
        now = self._now_ms()
        cold = self._cold_reader.read_revisions(instrument, timeframe, start_ts=start_ts, end_ts=now)
        hot = self._hot_bars.recent_bars(instrument_id=instrument.instrument_id, timeframe=timeframe) if self._hot_bars else ()
        rows = [*cold, *self._raw_store.revisions(instrument.symbol, timeframe), *hot]
        return [_received_kline(instrument.instrument_id, row) for row in rows if row.confirmed
                and row.ts_event >= start_ts and after < max(row.ts_ingested, row.ts_available) <= now]

    def _freeze(
        self,
        instrument: InstrumentSpec,
        request: SnapshotRequest,
        frames: Sequence[tuple[str, Sequence[SnapshotBar]]],
        cutoff: int,
        now: int,
        *,
        derivative_points: Sequence[DerivativePoint] = (),
        replay: dict[str, list[SnapshotEvent]] | None = None,
    ) -> SnapshotMeta:
        snapshot_id = uuid.uuid4().hex
        meta = SnapshotMeta(
            snapshot_id=snapshot_id,
            instrument_id=instrument.instrument_id,
            timeframes=list(request.timeframes),
            cutoff_ts=cutoff,
            created_ts=now,
            mode=request.mode,
            bar_counts={timeframe: len(entries) for timeframe, entries in frames},
        )
        committed_ts = max(
            (
                entry.ts_committed
                for _, entries in frames
                for entry in entries
                if entry.ts_committed is not None
            ),
            default=None,
        )
        parquet_path = write_snapshot_parquet(self._data_dir, snapshot_id, frames)
        derivatives_path = None
        try:
            if derivative_points:
                derivatives_path = write_derivative_parquet(
                    self._data_dir, snapshot_id, derivative_points
                )
            self._snapshot_store.register(
                meta,
                parquet_path,
                committed_ts=committed_ts,
                derivatives_path=derivatives_path, replay=replay,
            )
        except Exception:
            (self._data_dir / parquet_path).unlink(missing_ok=True)
            if derivatives_path is not None:
                (self._data_dir / derivatives_path).unlink(missing_ok=True)
            raise
        return meta

    def _derivative_points(
        self,
        *,
        instrument: InstrumentSpec,
        request: SnapshotRequest,
        cutoff_ts: int,
    ) -> list[DerivativePoint]:
        """物化冷库当时可见的历史序列，维持已登记快照的内容不变。"""
        connector = self._connector
        points: list[DerivativePoint] = []
        if self._cold_publisher is None and isinstance(connector, DerivativeDataConnector):
            for kind, timeframe in derivative_series(request.timeframes):
                try:
                    pages = derivative_history_pages(
                        connector, self._policy, symbol=instrument.symbol,
                        kind=kind, timeframe=timeframe, cutoff=cutoff_ts,
                    )
                    points.extend([row.to_point(instrument.instrument_id) for page in pages for row in page])
                except FeedUnavailableError as exc:
                    logger.warning("衍生品回填失败，快照继续（%s %s）：%s", kind, timeframe, exc)
        if self._cold_publisher is not None:
            future = self._cold_publisher.request_derivative_history(
                instrument, request.timeframes, cutoff_ts=cutoff_ts,
            )
            if future is not None:
                future.result()
        timeframes = {
            DerivativeKind.MARK_PRICE: set(request.timeframes),
            DerivativeKind.OPEN_INTEREST: {
                OI_TIMEFRAME_BY_TIMEFRAME[tf] for tf in request.timeframes if tf in OI_TIMEFRAME_BY_TIMEFRAME
            },
            DerivativeKind.FUNDING_RATE: {None},
        }
        for kind, wanted in timeframes.items():
            try:
                rows = self._cold_reader.read_derivative_revisions(
                    instrument, kind, end_ts=cutoff_ts,
                )
            except ColdStoreError as exc:
                logger.warning("冷库衍生品快照输入读取失败（%s %s）：%s", instrument.instrument_id, kind, exc)
                continue
            for timeframe in wanted | {None}:
                series = [
                    point for point in rows if point.timeframe == timeframe
                    and point.ts_available <= cutoff_ts
                ]
                points.extend(series)
        return points
