"""看盘 API 的应用服务：快照创建、读回与分页。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from talaria.foundation.bars import CompactBar
from talaria.foundation.connector import MarketDataConnector
from talaria.foundation.derivatives import (
    OI_TIMEFRAME_BY_TIMEFRAME,
    DerivativeBundle,
    DerivativeKind,
    DerivativePoint,
    DerivativeSeries,
    DerivativeSummary,
    latest_visible_derivatives,
)
from talaria.foundation.feed import KlineEvent
from talaria.foundation.drawings import DrawingRecord
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
    EvidenceClass,
    KeyLevel,
    KeyLevelCategory,
    KeyLevelParams,
    KeyLevelSet,
    compute_key_levels_with_notes,
    session_profile_from_trait,
)
from talaria.foundation.market import InstrumentSpec, MarketPack
from talaria.foundation.operators import (
    DERIVATIVE_INDICATORS,
    DerivativeSample,
    IndicatorSpec,
    compute_series,
    spec_from_params,
)
from talaria.foundation.operators.inputs import derivative_samples
from talaria.foundation.rules import RuleLevelEvent, RuleLevelPrice, RulePreviewRequest, RulePreviewResponse
from talaria.foundation.snapshot import (
    ChartPage,
    SnapshotMeta,
    SnapshotMode,
    SnapshotRequest,
)
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES, timeframe_ms
from talaria.perception.cold_publisher import ColdPublisher
from talaria.perception.cold_store import ColdReader
from talaria.perception.key_level_registry import FrozenLevelHistory, KeyLevelRegistry
from talaria.perception.snapshot_builder import SnapshotBuilder
from talaria.perception.snapshot_store import (
    SnapshotRecord,
    SnapshotStore,
    read_derivative_points,
    read_snapshot_bars,
)
from talaria.workbench_api.rule_preview import evaluate_preview

logger = logging.getLogger(__name__)


class WorkbenchService:
    def __init__(
        self,
        *,
        pack: MarketPack,
        connector: MarketDataConnector | None,
        data_dir: Path,
        snapshot_store: SnapshotStore,
        builder: SnapshotBuilder,
        key_levels: KeyLevelRegistry,
        feed_mode: str | None = None,
        cold_publisher: ColdPublisher | None = None,
    ) -> None:
        self._pack = pack
        self._connector = connector
        self._data_dir = data_dir
        self._snapshot_store = snapshot_store
        self._builder = builder
        self._feed_mode = feed_mode
        self._cold_publisher = cold_publisher
        self._key_levels = key_levels
        self._background_freeze = asyncio.Lock()

    @property
    def feed_mode(self) -> str:
        if self._feed_mode is not None:
            return self._feed_mode
        if self._connector is None:
            return "disabled"
        return getattr(self._connector, "feed_mode", "unknown")

    def instruments(self) -> Sequence[InstrumentSpec]:
        return self._pack.instruments()

    def create_snapshot(self, request: SnapshotRequest) -> SnapshotMeta:
        meta, versions = self._prepare_snapshot(request)
        for version in versions:
            self._key_levels.register(version)
        return meta

    async def create_snapshot_async(self, request: SnapshotRequest) -> SnapshotMeta:
        meta, versions = await asyncio.to_thread(self._prepare_snapshot, request)
        for version in versions:
            self._key_levels.register(version)
        return meta

    def _prepare_snapshot(self, request: SnapshotRequest) -> tuple[SnapshotMeta, list[KeyLevelSet]]:
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
        meta = self._builder.build(instrument=instrument, request=normalized)
        return meta, self._snapshot_key_levels(meta)

    def _snapshot_key_levels(self, meta: SnapshotMeta) -> list[KeyLevelSet]:
        return [
            self.get_key_levels(meta.snapshot_id, timeframe=timeframe)
            for timeframe in meta.timeframes
        ]

    async def freeze_key_levels(self, instrument_id: str, timeframe: str, *, cutoff_ts: int) -> None:
        request = SnapshotRequest(
            instrument_id=instrument_id, timeframes=[timeframe], cutoff_ts=cutoff_ts,
        )
        async with self._background_freeze:
            _, versions = await asyncio.to_thread(self._prepare_snapshot, request)
            for version in versions:
                self._key_levels.register(version)

    async def warm_key_levels(
        self, pairs: set[tuple[str, str]], *, cutoff_ts: int
    ) -> set[tuple[str, str]]:
        pending = set()
        reader = ColdReader(self._data_dir)
        for instrument_id, timeframe in sorted(pairs):
            instrument = self._pack.instrument(instrument_id)
            if instrument is None:
                continue
            try:
                if self._cold_publisher is not None:
                    bars = await asyncio.to_thread(
                        reader.read_bars, instrument, timeframe, as_of=cutoff_ts, end_ts=cutoff_ts,
                    )
                    if not bars:
                        pending.add((instrument_id, timeframe))
                        continue
                await self.freeze_key_levels(instrument_id, timeframe, cutoff_ts=cutoff_ts)
            except Exception:
                logger.exception("预热关键位失败：%s %s", instrument_id, timeframe)
        return pending

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
        if self._cold_publisher is not None:
            instrument = self._pack.instrument(meta.instrument_id)
            if instrument is not None:
                interval = timeframe_ms(timeframe)
                end = meta.cutoff_ts - interval
                if before_ts is not None:
                    end = min(end, before_ts - 1)
                end = end // interval * interval
                start = end - (limit - 1) * interval
                if window:
                    start = min(start, window[0].timestamp)
                self._cold_publisher.request_history(
                    instrument, timeframe, start_ts=start, end_ts=end,
                )
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

    def get_derivatives(
        self, snapshot_id: str, *, timeframe: str
    ) -> DerivativeBundle:
        """按快照 cutoff 严格截断衍生品读数；十进制统一序列化为字符串。"""
        record = self._snapshot_record(snapshot_id, timeframe)
        meta = record.meta
        series = self._derivative_series(record, timeframe)
        mark_points = series[DerivativeKind.MARK_PRICE].points
        funding_points = series[DerivativeKind.FUNDING_RATE].points
        oi_points = series[DerivativeKind.OPEN_INTEREST].points
        mark = mark_points[-1] if mark_points else None
        funding = funding_points[-1] if funding_points else None
        oi = oi_points[-1] if oi_points else None
        next_funding_ts = None
        if funding is not None:
            interval_hours = self._funding_interval_hours(meta.instrument_id)
            if interval_hours is not None and interval_hours > 0:
                interval_ms = interval_hours * 3_600_000
                next_funding_ts = funding.ts_event + interval_ms
                # 历史结算接口只回放已发生结算；按结算周期推进到快照截止后的下一次。
                while next_funding_ts <= meta.cutoff_ts:
                    next_funding_ts += interval_ms
        return DerivativeBundle(
            snapshot_id=snapshot_id,
            instrument_id=meta.instrument_id,
            timeframe=timeframe,
            cutoff_ts=meta.cutoff_ts,
            series=[
                series[DerivativeKind.MARK_PRICE],
                series[DerivativeKind.FUNDING_RATE],
                series[DerivativeKind.OPEN_INTEREST],
            ],
            summary=DerivativeSummary(
                mark_price=None if mark is None else mark.value,
                mark_price_ts=None if mark is None else mark.ts_event,
                funding_rate=None if funding is None else funding.value,
                funding_settled_ts=None if funding is None else funding.ts_event,
                next_funding_ts=next_funding_ts,
                open_interest=None if oi is None else oi.value,
                open_interest_ts=None if oi is None else oi.ts_event,
                oi_history_available=bool(oi_points),
            ),
        )

    def chart_rule_history(self, snapshot_id: str, instrument_id: str, timeframe: str) -> list[KlineEvent]:
        meta, bars = self._snapshot_bars(snapshot_id, timeframe)
        if meta.instrument_id != instrument_id:
            raise InvalidRequestError("指标起点快照不属于该标的")
        frozen = self._snapshot_store.replay_events(snapshot_id, timeframe)
        history = ([row.event for row in frozen if isinstance(row.event, KlineEvent) and row.event.confirmed]
                   if frozen is not None else
                   [KlineEvent(instrument_id=instrument_id, timeframe=timeframe, bar=bar, confirmed=True, revision=0)
                    for bar in bars])
        instrument = self._pack.instrument(instrument_id)
        if instrument is not None and bars:
            history.extend(self._builder.chart_updates(instrument, timeframe, bars[0].timestamp, meta.cutoff_ts))
        return history

    def preview_rule(
        self, snapshot_id: str, request: RulePreviewRequest,
        drawings: Mapping[str, Sequence[DrawingRecord]],
        *, check_cancelled: Callable[[], None] = lambda: None,
    ) -> RulePreviewResponse:
        record = self._snapshot_record(snapshot_id, request.timeframe)
        meta, bars = self._snapshot_bars(snapshot_id, request.timeframe)
        request = request.model_copy(deep=True)
        for operand in request.rule.indicators():
            if operand.window == "chart":
                operand.snapshot_id = snapshot_id
        refs = [ref for ref in request.rule.references() if isinstance(ref, (RuleLevelEvent, RuleLevelPrice))]
        level_history = None
        if refs:
            publications = self._snapshot_store.frozen_levels(meta, request.timeframe, check_cancelled)
            if not any(row.version is not None for row in publications):
                raise InvalidRequestError("关键位历史版本不可用")
            first_slot = bars[max(0, len(bars) - request.limit)].timestamp if bars else 0
            first_close = first_slot + (timeframe_ms(request.timeframe) if request.rule.timing == "bar_close" else 0)
            level_history = FrozenLevelHistory(publications, snapshot_id, first_close)
            for ref in refs:
                if ref.level_id not in level_history.known_ids:
                    raise InvalidRequestError(f"历史关键位 ID 不存在：{ref.level_id}")
        check_cancelled()
        events = self._snapshot_store.replay_events(snapshot_id, request.timeframe, check_cancelled)
        if events is None:
            raise InvalidRequestError("历史版本不可用：此快照未记录行情修订和接收时刻")
        result = evaluate_preview(
            request, meta.instrument_id, meta.cutoff_ts, bars,
            lambda: read_derivative_points(self._data_dir, record.derivatives_path) if record.derivatives_path else (),
            drawings, level_history.levels_at if level_history else lambda *_: (), events=events,
            check_cancelled=check_cancelled,
        )
        if level_history and level_history.missing and not level_history.covered:
            raise InvalidRequestError("关键位历史版本不可用")
        result.history_incomplete = bool(level_history and level_history.missing)
        return result

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
        samples = (
            self._derivative_samples(snapshot_id, timeframe, bars)
            if any(spec.name in DERIVATIVE_INDICATORS for spec in specs)
            else None
        )
        result = [
            self._series(meta, bars, spec, limit, derivatives=samples)
            for spec in specs
        ]
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
        samples = (
            self._derivative_samples(snapshot_id, timeframe, bars)
            if spec.name in DERIVATIVE_INDICATORS
            else None
        )
        return self._series(meta, bars, spec, limit, derivatives=samples)

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
        evidence_class: EvidenceClass | None = None,
    ) -> KeyLevelSet:
        """按快照 PIT 数据计算关键位；筛选与排序在确定性结果之上进行。"""
        record = self._snapshot_record(snapshot_id, timeframe)
        meta = record.meta
        instrument = self._pack.instrument(meta.instrument_id)
        if instrument is None:
            raise InstrumentNotFoundError(f"未知标的：{meta.instrument_id}")
        _, bars = self._snapshot_bars(snapshot_id, timeframe)
        traits = self._pack.traits(meta.instrument_id)
        profile = session_profile_from_trait(traits.session)
        engine_params = params or KeyLevelParams()
        derivative_points = [
            point
            for point in self._derivative_points(record, timeframe)
            if point.kind is DerivativeKind.OPEN_INTEREST
        ]
        computation = compute_key_levels_with_notes(
            instrument_id=meta.instrument_id,
            timeframe=timeframe,
            bars=bars,
            cutoff_ts=meta.cutoff_ts,
            session_profile=profile,
            params=engine_params,
            derivative_points=derivative_points,
        )
        selected = set(categories) if categories else None
        filtered = [
            level
            for level in computation.levels
            if (selected is None or level.category in selected)
            and level.strength >= min_strength
            and (evidence_class is None or level.evidence_class is evidence_class)
        ]
        filtered = _sort_levels(filtered, order)
        if top_n is not None:
            filtered = filtered[:top_n]
        notes = (
            []
            if evidence_class is EvidenceClass.OBSERVED
            else list(computation.notes)
        )
        return KeyLevelSet(
            snapshot_id=snapshot_id,
            instrument_id=meta.instrument_id,
            timeframe=timeframe,
            cutoff_ts=meta.cutoff_ts,
            as_of_ts=meta.cutoff_ts,
            method_version=KEY_LEVEL_METHOD_VERSION,
            parameters=engine_params.as_parameters(),
            notes=notes,
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

    def _snapshot_record(
        self, snapshot_id: str, timeframe: str
    ) -> SnapshotRecord:
        record = self._snapshot_store.get(snapshot_id)
        if record is None:
            raise SnapshotNotFoundError(f"未知快照：{snapshot_id}")
        if timeframe not in record.meta.timeframes:
            raise SnapshotNotFoundError(
                f"快照 {snapshot_id} 不包含周期 {timeframe}"
            )
        return record

    def _derivative_points(
        self, record: SnapshotRecord, timeframe: str
    ) -> list[DerivativePoint]:
        if record.derivatives_path is None:
            return []
        points = read_derivative_points(self._data_dir, record.derivatives_path)
        cutoff = record.meta.cutoff_ts
        oi_timeframe = OI_TIMEFRAME_BY_TIMEFRAME.get(timeframe, timeframe)
        visible: list[DerivativePoint] = []
        for point in latest_visible_derivatives(points, as_of=cutoff):
            if point.kind is DerivativeKind.FUNDING_RATE and not point.is_funding_settlement:
                continue
            if point.kind is DerivativeKind.MARK_PRICE:
                if point.timeframe != timeframe:
                    continue
            elif point.kind is DerivativeKind.OPEN_INTEREST:
                if point.timeframe != oi_timeframe:
                    continue
            visible.append(point)
        return visible

    def _derivative_series(
        self, record: SnapshotRecord, timeframe: str
    ) -> dict[DerivativeKind, DerivativeSeries]:
        grouped: dict[DerivativeKind, list[DerivativePoint]] = {
            DerivativeKind.MARK_PRICE: [],
            DerivativeKind.FUNDING_RATE: [],
            DerivativeKind.OPEN_INTEREST: [],
        }
        for point in self._derivative_points(record, timeframe):
            grouped[point.kind].append(point)
        return {
            kind: DerivativeSeries(
                kind=kind,
                timeframe=self._series_timeframe(record, timeframe, kind),
                points=sorted(values, key=lambda point: point.ts_event),
            )
            for kind, values in grouped.items()
        }

    @staticmethod
    def _series_timeframe(
        record: SnapshotRecord, timeframe: str, kind: DerivativeKind
    ) -> str | None:
        if kind is DerivativeKind.MARK_PRICE:
            return timeframe
        if kind is DerivativeKind.OPEN_INTEREST:
            return OI_TIMEFRAME_BY_TIMEFRAME.get(timeframe, timeframe)
        return None

    def _derivative_samples(
        self,
        snapshot_id: str,
        timeframe: str,
        bars: Sequence[CompactBar],
    ) -> list[DerivativeSample]:
        """把结算/OI 点按 ``ts_available`` 向前取值，逐根对齐到 K 线。"""
        record = self._snapshot_record(snapshot_id, timeframe)
        series = self._derivative_series(record, timeframe)
        return derivative_samples(
            bars, series[DerivativeKind.FUNDING_RATE].points,
            series[DerivativeKind.OPEN_INTEREST].points,
        )

    def _funding_interval_hours(self, instrument_id: str) -> int | None:
        try:
            traits = self._pack.traits(instrument_id)
        except KeyError:
            return None
        return traits.carry.funding_interval_hours

    @staticmethod
    def _series(
        meta: SnapshotMeta,
        bars: Sequence[CompactBar],
        spec: IndicatorSpec,
        limit: int,
        *,
        derivatives: Sequence[DerivativeSample] | None = None,
    ) -> IndicatorSeries:
        state = compute_series(bars, spec, derivatives=derivatives)
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
