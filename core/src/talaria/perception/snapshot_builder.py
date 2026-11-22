"""快照编排：确保历史 → 合并热端切片 → 按 cutoff 紧凑化 → 原子写 Parquet → 登记清单。"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Callable, Sequence

from talaria.foundation.bars import CompactBar
from talaria.foundation.connector import MarketDataConnector, RawBar
from talaria.foundation.derivatives import (
    OI_TIMEFRAME_BY_TIMEFRAME,
    DerivativeDataConnector,
    DerivativePoint,
    RawDerivativePoint,
)
from talaria.foundation.errors import FeatureDisabledError, FeedUnavailableError
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.snapshot import SnapshotMeta, SnapshotRequest
from talaria.perception.backfill import BackfillPolicy, ensure_history
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
    ) -> None:
        self._data_dir = data_dir
        self._connector = connector
        self._raw_store = raw_store
        self._snapshot_store = snapshot_store
        self._policy = policy
        self._now_ms = now_ms
        self._hot_bars = hot_bars

    def build(
        self,
        *,
        instrument: InstrumentSpec,
        request: SnapshotRequest,
    ) -> SnapshotMeta:
        now = self._now_ms()
        cutoff = request.cutoff_ts if request.cutoff_ts is not None else now
        snapshot_id = uuid.uuid4().hex
        frames: list[tuple[str, Sequence[SnapshotBar]]] = []
        bar_counts: dict[str, int] = {}
        committed_ts: int | None = None

        for timeframe in request.timeframes:
            if self._connector is not None:
                ensure_history(
                    raw_store=self._raw_store,
                    connector=self._connector,
                    symbol=instrument.symbol,
                    timeframe=timeframe,
                    cutoff_ts=cutoff,
                    policy=self._policy,
                )
            archived = self._raw_store.revisions(instrument.symbol, timeframe)
            hot = (
                self._hot_bars.recent_bars(
                    instrument_id=instrument.instrument_id, timeframe=timeframe
                )
                if self._hot_bars is not None
                else ()
            )
            revisions = [*archived, *hot]
            raw_bars = latest_visible_bars(revisions, as_of=cutoff)
            if self._connector is None and not raw_bars:
                raise FeatureDisabledError(
                    f"行情采集已在 service.json 中关闭，缺少本地历史："
                    f"{instrument.instrument_id} {timeframe}（截至 {cutoff}）"
                )
            compact = compact_series(raw_bars, timeframe=timeframe, cutoff_ts=cutoff)
            entries = _snapshot_entries(compact, raw_bars, cutoff_ts=cutoff)
            frames.append((timeframe, entries))
            bar_counts[timeframe] = len(compact)
            if entries:
                frame_committed = max(
                    entry.ts_committed
                    for entry in entries
                    if entry.ts_committed is not None
                )
                committed_ts = (
                    frame_committed
                    if committed_ts is None
                    else max(committed_ts, frame_committed)
                )

        meta = SnapshotMeta(
            snapshot_id=snapshot_id,
            instrument_id=instrument.instrument_id,
            timeframes=list(request.timeframes),
            cutoff_ts=cutoff,
            created_ts=now,
            mode=request.mode,
            bar_counts=bar_counts,
        )
        parquet_path = write_snapshot_parquet(self._data_dir, snapshot_id, frames)
        derivatives_path = None
        try:
            derivative_points = self._derivative_points(
                instrument=instrument, request=request, cutoff_ts=cutoff
            )
            if derivative_points:
                derivatives_path = write_derivative_parquet(
                    self._data_dir, snapshot_id, derivative_points
                )
            self._snapshot_store.register(
                meta,
                parquet_path,
                committed_ts=committed_ts,
                derivatives_path=derivatives_path,
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
        """按各接口游标语义向左回填；任何衍生品缺失都不阻塞主快照。"""
        connector = self._connector
        if not isinstance(connector, DerivativeDataConnector):
            return []
        collected: dict[tuple[str, str | None, int], RawDerivativePoint] = {}

        def collect(points: Sequence[RawDerivativePoint]) -> None:
            for point in points:
                if point.ts_available > cutoff_ts:
                    continue
                collected[(point.kind.value, point.timeframe, point.ts_event)] = point

        for timeframe in request.timeframes:
            try:
                collect(
                    self._pages(
                        lambda end_ts, limit, tf=timeframe: connector.fetch_mark_price_bars(
                            symbol=instrument.symbol,
                            timeframe=tf,
                            end_ts=end_ts,
                            limit=limit,
                        ),
                        target=self._policy.target_bars,
                        page_limit=min(self._policy.page_limit, 1000),
                        cutoff_ts=cutoff_ts,
                    )
                )
            except FeedUnavailableError as exc:
                logger.warning("标记价回填失败，快照继续：%s", exc)
        try:
            collect(
                self._pages(
                    lambda end_ts, limit: connector.fetch_funding_settlements(
                        symbol=instrument.symbol, end_ts=end_ts, limit=limit
                    ),
                    target=max(8, min(1000, self._policy.target_bars // 8)),
                    page_limit=200,
                    cutoff_ts=cutoff_ts,
                )
            )
        except FeedUnavailableError as exc:
            logger.warning("资金费率回填失败，快照继续：%s", exc)
        oi_timeframes = sorted(
            {
                OI_TIMEFRAME_BY_TIMEFRAME[timeframe]
                for timeframe in request.timeframes
                if timeframe in OI_TIMEFRAME_BY_TIMEFRAME
            }
        )
        for oi_timeframe in oi_timeframes:
            try:
                collect(
                    self._pages(
                        lambda end_ts, limit, tf=oi_timeframe: connector.fetch_open_interest(
                            symbol=instrument.symbol,
                            timeframe=tf,
                            end_ts=end_ts,
                            limit=limit,
                        ),
                        target=self._policy.target_bars,
                        page_limit=200,
                        cutoff_ts=cutoff_ts,
                    )
                )
            except FeedUnavailableError as exc:
                logger.warning("持仓量回填失败，快照继续：%s", exc)

        points = [
            point.to_point(instrument.instrument_id)
            for point in sorted(
                collected.values(),
                key=lambda item: (item.kind.value, item.timeframe or "", item.ts_event),
            )
        ]
        return points

    def _pages(
        self,
        fetch,
        *,
        target: int,
        page_limit: int,
        cutoff_ts: int,
    ) -> list[RawDerivativePoint]:
        """复用 04 的向左游标语义：从 cutoff 开始，按最老事件继续翻页。"""
        collected: dict[int, RawDerivativePoint] = {}
        cursor: int | None = cutoff_ts
        for _ in range(self._policy.max_pages):
            batch = [
                point
                for point in fetch(end_ts=cursor, limit=page_limit)
                if point.ts_available <= cutoff_ts
            ]
            before = len(collected)
            for point in batch:
                collected[point.ts_event] = point
            if len(collected) == before or len(collected) >= target:
                break
            oldest = min(collected)
            if oldest <= 0:
                break
            cursor = oldest - 1
            if len(batch) < page_limit:
                break
        return [collected[key] for key in sorted(collected)]
