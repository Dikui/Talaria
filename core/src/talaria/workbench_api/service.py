"""看盘 API 的应用服务：快照创建、读回与分页。"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from talaria.foundation.connector import MarketDataConnector
from talaria.foundation.errors import (
    FeatureNotImplementedError,
    InstrumentNotFoundError,
    InvalidRequestError,
    SnapshotNotFoundError,
)
from talaria.foundation.market import InstrumentSpec, MarketPack
from talaria.foundation.snapshot import (
    ChartPage,
    SnapshotMeta,
    SnapshotMode,
    SnapshotRequest,
)
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES
from talaria.perception.snapshot_builder import SnapshotBuilder
from talaria.perception.snapshot_store import SnapshotStore, read_snapshot_bars


class WorkbenchService:
    def __init__(
        self,
        *,
        pack: MarketPack,
        connector: MarketDataConnector,
        data_dir: Path,
        snapshot_store: SnapshotStore,
        builder: SnapshotBuilder,
    ) -> None:
        self._pack = pack
        self._connector = connector
        self._data_dir = data_dir
        self._snapshot_store = snapshot_store
        self._builder = builder

    @property
    def feed_mode(self) -> str:
        return getattr(self._connector, "feed_mode", "unknown")

    def instruments(self) -> Sequence[InstrumentSpec]:
        return self._pack.instruments()

    def create_snapshot(self, request: SnapshotRequest) -> SnapshotMeta:
        if request.mode is SnapshotMode.OBSERVED_REPLAY:
            raise FeatureNotImplementedError(
                "OBSERVED_REPLAY 快照将在 M3 提供；当前仅支持 HISTORICAL_PIT"
            )
        instrument = self._pack.instrument(request.instrument_id)
        if instrument is None:
            raise InstrumentNotFoundError(f"未知标的：{request.instrument_id}")
        timeframes = list(dict.fromkeys(request.timeframes))
        for timeframe in timeframes:
            if timeframe not in SUPPORTED_TIMEFRAMES:
                raise InvalidRequestError(f"不支持的周期：{timeframe}")
        normalized = request.model_copy(update={"timeframes": timeframes})
        return self._builder.build(instrument=instrument, request=normalized)

    def get_snapshot(self, snapshot_id: str) -> SnapshotMeta:
        record = self._snapshot_store.get(snapshot_id)
        if record is None:
            raise SnapshotNotFoundError(f"未知快照：{snapshot_id}")
        return record.meta

    def get_bars(
        self,
        snapshot_id: str,
        *,
        timeframe: str,
        limit: int,
        before_ts: int | None,
    ) -> ChartPage:
        record = self._snapshot_store.get(snapshot_id)
        if record is None:
            raise SnapshotNotFoundError(f"未知快照：{snapshot_id}")
        meta = record.meta
        if timeframe not in meta.timeframes:
            raise SnapshotNotFoundError(f"快照 {snapshot_id} 不包含周期 {timeframe}")

        all_bars = read_snapshot_bars(self._data_dir, record.parquet_path, timeframe)
        eligible = [
            bar for bar in all_bars if before_ts is None or bar.timestamp < before_ts
        ]
        window = eligible[-limit:]
        has_more_forward = len(eligible) > len(window)
        has_more_backward = bool(
            all_bars
            and (
                not window
                or all_bars[-1].timestamp > window[-1].timestamp
            )
        )
        return ChartPage(
            snapshot_id=snapshot_id,
            instrument_id=meta.instrument_id,
            timeframe=timeframe,
            bars=window,
            has_more_forward=has_more_forward,
            has_more_backward=has_more_backward,
        )
