"""快照编排：确保历史 → 按 cutoff 紧凑化 → 原子写 Parquet → 登记清单。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Callable, Sequence

from talaria.foundation.bars import CompactBar
from talaria.foundation.connector import MarketDataConnector
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.snapshot import SnapshotMeta, SnapshotRequest
from talaria.perception.backfill import BackfillPolicy, ensure_history
from talaria.perception.compaction import compact_series
from talaria.perception.raw_store import RawStore
from talaria.perception.snapshot_store import SnapshotStore, write_snapshot_parquet


class SnapshotBuilder:
    def __init__(
        self,
        *,
        data_dir: Path,
        connector: MarketDataConnector,
        raw_store: RawStore,
        snapshot_store: SnapshotStore,
        policy: BackfillPolicy,
        now_ms: Callable[[], int],
    ) -> None:
        self._data_dir = data_dir
        self._connector = connector
        self._raw_store = raw_store
        self._snapshot_store = snapshot_store
        self._policy = policy
        self._now_ms = now_ms

    def build(
        self,
        *,
        instrument: InstrumentSpec,
        request: SnapshotRequest,
    ) -> SnapshotMeta:
        now = self._now_ms()
        cutoff = request.cutoff_ts if request.cutoff_ts is not None else now
        snapshot_id = uuid.uuid4().hex
        frames: list[tuple[str, Sequence[CompactBar]]] = []
        bar_counts: dict[str, int] = {}

        for timeframe in request.timeframes:
            ensure_history(
                raw_store=self._raw_store,
                connector=self._connector,
                symbol=instrument.symbol,
                timeframe=timeframe,
                cutoff_ts=cutoff,
                policy=self._policy,
            )
            raw_bars = self._raw_store.read(instrument.symbol, timeframe)
            compact = compact_series(raw_bars, timeframe=timeframe, cutoff_ts=cutoff)
            frames.append((timeframe, compact))
            bar_counts[timeframe] = len(compact)

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
        try:
            self._snapshot_store.register(meta, parquet_path)
        except Exception:
            (self._data_dir / parquet_path).unlink(missing_ok=True)
            raise
        return meta
