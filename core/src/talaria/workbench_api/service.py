"""看盘 API 的应用服务：快照创建、读回与分页。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from talaria.foundation.bars import CompactBar
from talaria.foundation.connector import MarketDataConnector
from talaria.foundation.errors import (
    FeatureNotImplementedError,
    InstrumentNotFoundError,
    InvalidRequestError,
    SnapshotNotFoundError,
)
from talaria.foundation.indicators import (
    IndicatorBundle,
    IndicatorName,
    IndicatorParams,
    IndicatorPoint,
    IndicatorSeries,
    IndicatorSummary,
)
from talaria.foundation.key_levels import (
    KEY_LEVEL_METHOD_VERSION,
    KeyLevel,
    KeyLevelCategory,
    KeyLevelParams,
    KeyLevelSet,
    compute_key_levels,
    session_profile_from_trait,
)
from talaria.foundation.market import InstrumentSpec, MarketPack
from talaria.foundation.operators import (
    IndicatorSpec,
    compute_series,
    spec_from_params,
)
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
        feed_mode: str | None = None,
    ) -> None:
        self._pack = pack
        self._connector = connector
        self._data_dir = data_dir
        self._snapshot_store = snapshot_store
        self._builder = builder
        self._feed_mode = feed_mode

    @property
    def feed_mode(self) -> str:
        if self._feed_mode is not None:
            return self._feed_mode
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
        meta, all_bars = self._snapshot_bars(snapshot_id, timeframe)
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

    def get_indicators(
        self,
        snapshot_id: str,
        *,
        timeframe: str,
        names: Sequence[str],
        limit: int,
        params: Mapping[str, IndicatorParams | Mapping[str, Any]] | None = None,
        extra_bars: Sequence[CompactBar] = (),
    ) -> IndicatorBundle:
        """默认参数计算一组指标；窗口取快照末尾，但预热使用完整历史。

        ``extra_bars`` 只用于 WS bar 触发的降级增量路径：快照仍是历史真源，
        实时尾段追加在其后参与同一套算子计算。
        """
        meta, bars = self._snapshot_bars(snapshot_id, timeframe)
        bars = self._with_extra_bars(bars, extra_bars)
        unique_names = list(dict.fromkeys(names))
        specs: list[IndicatorSpec] = []
        for name in unique_names:
            raw = params.get(name) if params is not None else None
            if isinstance(raw, IndicatorParams):
                raw = raw.model_dump(exclude_none=True)
            try:
                specs.append(spec_from_params(name, raw))
            except ValueError as exc:
                raise InvalidRequestError(str(exc)) from exc
        result = [self._series(meta, bars, spec, limit) for spec in specs]
        return IndicatorBundle(
            snapshot_id=snapshot_id,
            instrument_id=meta.instrument_id,
            timeframe=timeframe,
            series=result,
        )

    def compute_operator(
        self,
        snapshot_id: str,
        *,
        timeframe: str,
        operator: IndicatorName | str,
        limit: int,
        params: IndicatorParams | Mapping[str, Any] | None = None,
        extra_bars: Sequence[CompactBar] = (),
    ) -> IndicatorSeries:
        """允许非默认参数的单指标计算；语义与批量指标完全一致。"""
        meta, bars = self._snapshot_bars(snapshot_id, timeframe)
        bars = self._with_extra_bars(bars, extra_bars)
        if isinstance(params, IndicatorParams):
            params = params.model_dump(exclude_none=True)
        try:
            spec = spec_from_params(operator, params)
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from exc
        return self._series(meta, bars, spec, limit)

    def get_key_levels(
        self,
        snapshot_id: str,
        *,
        timeframe: str,
        categories: Sequence[KeyLevelCategory] | None = None,
        min_strength: float = 0.0,
        order: str = "strength_desc",
        top_n: int | None = None,
        params: KeyLevelParams | None = None,
    ) -> KeyLevelSet:
        """按快照 PIT 数据计算关键位；筛选与排序在确定性结果之上进行。"""
        meta, bars = self._snapshot_bars(snapshot_id, timeframe)
        instrument = self._pack.instrument(meta.instrument_id)
        if instrument is None:
            raise InstrumentNotFoundError(f"未知标的：{meta.instrument_id}")
        traits = self._pack.traits(meta.instrument_id)
        profile = session_profile_from_trait(traits.session)
        engine_params = params or KeyLevelParams()
        levels = compute_key_levels(
            instrument_id=meta.instrument_id,
            timeframe=timeframe,
            bars=bars,
            cutoff_ts=meta.cutoff_ts,
            session_profile=profile,
            params=engine_params,
        )
        selected = set(categories) if categories else None
        filtered = [
            level
            for level in levels
            if (selected is None or level.category in selected)
            and level.strength >= min_strength
        ]
        filtered = _sort_levels(filtered, order)
        if top_n is not None:
            filtered = filtered[:top_n]
        return KeyLevelSet(
            snapshot_id=snapshot_id,
            instrument_id=meta.instrument_id,
            timeframe=timeframe,
            cutoff_ts=meta.cutoff_ts,
            as_of_ts=meta.cutoff_ts,
            method_version=KEY_LEVEL_METHOD_VERSION,
            parameters=engine_params.as_parameters(),
            levels=filtered,
        )

    @staticmethod
    def _with_extra_bars(
        bars: Sequence[CompactBar], extra_bars: Sequence[CompactBar]
    ) -> list[CompactBar]:
        if not extra_bars:
            return list(bars)
        merged = {bar.timestamp: bar for bar in bars}
        for bar in extra_bars:
            merged[bar.timestamp] = bar
        return [merged[timestamp] for timestamp in sorted(merged)]

    def _snapshot_bars(
        self, snapshot_id: str, timeframe: str
    ) -> tuple[SnapshotMeta, list[CompactBar]]:
        record = self._snapshot_store.get(snapshot_id)
        if record is None:
            raise SnapshotNotFoundError(f"未知快照：{snapshot_id}")
        meta = record.meta
        if timeframe not in meta.timeframes:
            raise SnapshotNotFoundError(f"快照 {snapshot_id} 不包含周期 {timeframe}")
        bars = [
            bar
            for bar in read_snapshot_bars(self._data_dir, record.parquet_path, timeframe)
            if bar.ts_available <= meta.cutoff_ts
        ]
        return meta, bars

    @staticmethod
    def _series(
        meta: SnapshotMeta,
        bars: Sequence[CompactBar],
        spec: IndicatorSpec,
        limit: int,
    ) -> IndicatorSeries:
        state = compute_series(bars, spec)
        window_bars = bars[-limit:]
        window_points = state.points[-limit:]
        points: list[IndicatorPoint] = []
        for bar, values in zip(window_bars, window_points, strict=True):
            points.append(IndicatorPoint(timestamp=bar.timestamp, values=dict(values)))
        latest = dict(points[-1].values) if points else {c: None for c in spec.channels}
        warmup_bars = next(
            (
                index
                for index, point in enumerate(points)
                if all(value is not None for value in point.values.values())
            ),
            len(points),
        )
        valid_points = sum(
            1
            for point in points
            if all(value is not None for value in point.values.values())
        )
        return IndicatorSeries(
            name=spec.name,
            pane="sub" if spec.pane == "sub" else "main",
            params=IndicatorParams(
                period=int(spec.period),
                std_dev=spec.std_dev,
                anchor=spec.anchor,
            ),
            points=points,
            summary=IndicatorSummary(
                latest=latest,
                warmup_bars=warmup_bars,
                valid_points=valid_points,
            ),
        )


def _level_price(level: KeyLevel) -> Decimal:
    if level.price is not None:
        return level.price
    assert level.price_low is not None and level.price_high is not None
    return (level.price_low + level.price_high) / 2


def _sort_levels(levels: list[KeyLevel], order: str) -> list[KeyLevel]:
    if order == "strength_desc":
        return sorted(
            levels, key=lambda item: (-item.strength, -item.ts_available, item.level_id)
        )
    if order == "strength_asc":
        return sorted(
            levels, key=lambda item: (item.strength, -item.ts_available, item.level_id)
        )
    if order == "price_asc":
        return sorted(levels, key=lambda item: (_level_price(item), item.level_id))
    if order == "price_desc":
        return sorted(levels, key=lambda item: (-_level_price(item), item.level_id))
    if order == "ts_available_desc":
        return sorted(levels, key=lambda item: (-item.ts_available, item.level_id))
    raise InvalidRequestError(f"不支持的排序：{order}")
