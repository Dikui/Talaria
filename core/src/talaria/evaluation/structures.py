"""统一市场结构离线质量评估：逐柱 PIT 重放，支持任意几何原语与动态注册检测器。"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from talaria.evaluation.key_levels import (
    DEFAULT_ATR_MULTIPLE,
    DEFAULT_ATR_PERIOD,
    DEFAULT_BASELINE_SAMPLES,
    DEFAULT_HORIZON_BARS,
    DEFAULT_SEED,
    EvaluationError,
    _utc_iso,
)
from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.key_levels.contracts import SessionProfile
from talaria.foundation.operators import IndicatorName, IndicatorSpec, compute_series
from talaria.foundation.structures import (
    STRUCTURE_METHOD_VERSION,
    DetectorRegistry,
    HorizontalLineGeometry,
    HorizontalZoneGeometry,
    PriceScaleMode,
    SlopeLineGeometry,
    StructureAnchor,
    StructureFamily,
    StructureFeature,
    StructureGeometry,
    StructureParams,
    StructureStatus,
    compute_structure_set,
    default_structure_registry,
    geometry_bounds_at,
    geometry_distance,
)
from talaria.foundation.timeframes import timeframe_ms


@dataclass(frozen=True, slots=True)
class StructureEvaluationConfig:
    """市场结构离线评估配置；固定 ``seed`` 保证随机基线可复现。"""

    instrument_id: str
    timeframe: str
    from_ts: int
    to_ts: int
    atr_multiple: float = DEFAULT_ATR_MULTIPLE
    horizon_bars: int = DEFAULT_HORIZON_BARS
    baseline_samples: int = DEFAULT_BASELINE_SAMPLES
    seed: int = DEFAULT_SEED
    atr_period: int = DEFAULT_ATR_PERIOD
    detector_ids: tuple[str, ...] | None = None
    params: StructureParams | None = None

    def __post_init__(self) -> None:
        if not self.instrument_id:
            raise EvaluationError("invalid_argument", "instrument_id 不能为空")
        try:
            timeframe_ms(self.timeframe)
        except KeyError as exc:
            raise EvaluationError("unsupported_timeframe", f"不支持的周期: {self.timeframe!r}") from exc
        if self.from_ts > self.to_ts:
            raise EvaluationError("invalid_argument", "from_ts 不能晚于 to_ts")
        if not math.isfinite(self.atr_multiple) or self.atr_multiple <= 0:
            raise EvaluationError("invalid_argument", "atr_multiple 必须为正数")
        if self.horizon_bars <= 0:
            raise EvaluationError("invalid_argument", "horizon_bars 必须为正整数")
        if self.baseline_samples < 0:
            raise EvaluationError("invalid_argument", "baseline_samples 不能为负")
        if self.atr_period <= 1:
            raise EvaluationError("invalid_argument", "atr_period 必须大于 1")


@dataclass(frozen=True, slots=True)
class _Reaction:
    reacted: bool
    excursion_atr: float


@dataclass(frozen=True, slots=True)
class DetectorKindEvaluation:
    """按 ``(detector_id, kind)`` 分桶的统计结果及随机基线对照。"""

    detector_id: str
    kind: str
    family: str
    sample_count: int
    reactions: int
    mean_reaction_atr: float | None
    breakout_count: int
    breakout_followthroughs: int
    skipped_count: int
    baseline_sample_count: int
    baseline_reactions: int
    baseline_mean_reaction_atr: float | None
    baseline_skipped_count: int

    @property
    def reaction_rate(self) -> float | None:
        return None if self.sample_count == 0 else self.reactions / self.sample_count

    @property
    def breakout_followthrough_rate(self) -> float | None:
        return None if self.breakout_count == 0 else self.breakout_followthroughs / self.breakout_count

    @property
    def baseline_reaction_rate(self) -> float | None:
        return (
            None
            if self.baseline_sample_count == 0
            else self.baseline_reactions / self.baseline_sample_count
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "detector_id": self.detector_id,
            "kind": self.kind,
            "family": self.family,
            "sample_count": self.sample_count,
            "reaction_count": self.reactions,
            "reaction_rate": _rounded(self.reaction_rate),
            "mean_reaction_atr": _rounded(self.mean_reaction_atr),
            "breakout_count": self.breakout_count,
            "breakout_followthrough_count": self.breakout_followthroughs,
            "breakout_followthrough_rate": _rounded(self.breakout_followthrough_rate),
            "skipped_count": self.skipped_count,
            "baseline_sample_count": self.baseline_sample_count,
            "baseline_reaction_count": self.baseline_reactions,
            "baseline_reaction_rate": _rounded(self.baseline_reaction_rate),
            "baseline_mean_reaction_atr": _rounded(self.baseline_mean_reaction_atr),
            "baseline_skipped_count": self.baseline_skipped_count,
        }


@dataclass(frozen=True, slots=True)
class StructureEvaluationReport:
    """完整结构评估报告，包含分桶指标与相邻柱结构换手率（Churn Rate）。"""

    config: StructureEvaluationConfig
    results: tuple[DetectorKindEvaluation, ...]
    churn_rate: float | None
    skipped: int
    baseline_skipped: int

    @property
    def total_sample_count(self) -> int:
        return sum(row.sample_count for row in self.results)

    def as_dict(self) -> dict[str, object]:
        return {
            "instrument_id": self.config.instrument_id,
            "timeframe": self.config.timeframe,
            "from_ts": self.config.from_ts,
            "to_ts": self.config.to_ts,
            "from_iso": _utc_iso(self.config.from_ts),
            "to_iso": _utc_iso(self.config.to_ts),
            "atr_period": self.config.atr_period,
            "atr_multiple": self.config.atr_multiple,
            "horizon_bars": self.config.horizon_bars,
            "baseline_samples": self.config.baseline_samples,
            "seed": self.config.seed,
            "method_version": STRUCTURE_METHOD_VERSION,
            "churn_rate": _rounded(self.churn_rate),
            "skipped": self.skipped,
            "baseline_skipped": self.baseline_skipped,
            "total_sample_count": self.total_sample_count,
            "results": [row.as_dict() for row in self.results],
        }


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _baseline_seed(seed: int, key: str) -> int:
    digest = hashlib.sha256(f"{seed}:structure:{key}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _first_touch(
    bars: Sequence[CompactBar],
    start_index: int,
    geometry: StructureGeometry,
    *,
    until_ts: int,
) -> int | None:
    for idx in range(start_index, len(bars)):
        bar = bars[idx]
        if bar.timestamp > until_ts:
            break
        if bar.status is BarStatus.FEED_GAP or bar.high is None or bar.low is None:
            continue
        bounds = geometry_bounds_at(geometry, bar.timestamp)
        if bounds is None:
            continue
        if bar.low <= bounds.upper and bar.high >= bounds.lower:
            return idx
    return None


def _measure_reaction(
    bars: Sequence[CompactBar],
    atr_values: Sequence[Decimal | None],
    touch_index: int,
    geometry: StructureGeometry,
    *,
    horizon_bars: int,
    atr_multiple: Decimal,
) -> _Reaction | None:
    atr = atr_values[touch_index]
    if atr is None or atr <= 0:
        return None
    if touch_index + horizon_bars >= len(bars):
        return None

    max_dist = Decimal("0")
    for idx in range(touch_index + 1, touch_index + horizon_bars + 1):
        bar = bars[idx]
        if bar.close is None:
            continue
        bounds = geometry_bounds_at(geometry, bar.timestamp)
        if bounds is None:
            continue
        dist = geometry_distance(bar.close, bounds)
        if dist > max_dist:
            max_dist = dist
    excursion = float(max_dist / atr)
    return _Reaction(reacted=max_dist >= atr * atr_multiple, excursion_atr=excursion)


def _measure_breakout_followthrough(
    bars: Sequence[CompactBar],
    atr_values: Sequence[Decimal | None],
    start_index: int,
    geometry: StructureGeometry,
    *,
    until_ts: int,
    horizon_bars: int,
    atr_multiple: Decimal,
) -> bool | None:
    """查找首次收盘突破柱，并在后续 ``horizon_bars`` 内判定是否沿同向走出足够 ATR。"""

    break_idx: int | None = None
    direction: str | None = None
    for idx in range(start_index, len(bars)):
        bar = bars[idx]
        if bar.timestamp > until_ts:
            break
        if bar.close is None:
            continue
        bounds = geometry_bounds_at(geometry, bar.timestamp)
        if bounds is None:
            continue
        if bar.close > bounds.upper:
            break_idx = idx
            direction = "up"
            break
        if bar.close < bounds.lower:
            break_idx = idx
            direction = "down"
            break

    if break_idx is None or direction is None:
        return None
    atr = atr_values[break_idx]
    if atr is None or atr <= 0 or break_idx + horizon_bars >= len(bars):
        return None
    break_close = bars[break_idx].close
    assert break_close is not None
    threshold = atr * atr_multiple
    for idx in range(break_idx + 1, break_idx + horizon_bars + 1):
        bar = bars[idx]
        if bar.close is None:
            continue
        move = bar.close - break_close if direction == "up" else break_close - bar.close
        if move >= threshold:
            return True
    return False


def evaluate_structures(
    bars: Sequence[CompactBar],
    config: StructureEvaluationConfig,
    *,
    session_profile: SessionProfile | None = None,
    registry: DetectorRegistry | None = None,
) -> StructureEvaluationReport:
    """逐柱 PIT 重放统一市场结构引擎，计算反应率、突破延续率与结构换手率。"""

    ordered = tuple(sorted(bars, key=lambda bar: bar.timestamp))
    active_registry = registry or default_structure_registry()
    atr_state = compute_series(
        ordered, IndicatorSpec(name=IndicatorName.ATR, period=config.atr_period)
    )
    atr_values: tuple[Decimal | None, ...] = tuple(
        point.get("value") for point in atr_state.points
    )

    window_indices = [
        idx for idx, bar in enumerate(ordered) if config.from_ts <= bar.timestamp <= config.to_ts
    ]
    cutoff_indices = [
        idx for idx in window_indices if ordered[idx].status is not BarStatus.FEED_GAP
    ]

    discovered: dict[str, StructureFeature] = {}
    prev_active_ids: set[str] | None = None
    churn_samples: list[float] = []

    for idx in cutoff_indices:
        cutoff = ordered[idx].ts_available
        visible = [bar for bar in ordered if bar.ts_available <= cutoff]
        struct_set = compute_structure_set(
            instrument_id=config.instrument_id,
            timeframe=config.timeframe,
            bars=visible,
            cutoff_ts=cutoff,
            session_profile=session_profile,
            params=config.params,
            detector_ids=config.detector_ids,
            registry=active_registry,
        )
        current_ids: set[str] = set()
        for feat in struct_set.structures:
            if feat.ts_available > cutoff:
                continue
            discovered.setdefault(feat.structure_id, feat)
            if feat.status is StructureStatus.ACTIVE:
                current_ids.add(feat.structure_id)
        if prev_active_ids is not None and (prev_active_ids or current_ids):
            union_size = len(prev_active_ids | current_ids)
            diff_size = len(prev_active_ids ^ current_ids)
            churn_samples.append(diff_size / union_size if union_size > 0 else 0.0)
        prev_active_ids = current_ids

    buckets: dict[tuple[str, str], str] = {}
    for feat in discovered.values():
        buckets.setdefault((feat.detector_id, feat.kind), feat.family.value)

    ordered_buckets = sorted(buckets)
    real: dict[tuple[str, str], list[_Reaction]] = {k: [] for k in ordered_buckets}
    real_skipped: dict[tuple[str, str], int] = {k: 0 for k in ordered_buckets}
    breakouts: dict[tuple[str, str], list[bool]] = {k: [] for k in ordered_buckets}
    baseline: dict[tuple[str, str], list[_Reaction]] = {k: [] for k in ordered_buckets}
    baseline_skipped: dict[tuple[str, str], int] = {k: 0 for k in ordered_buckets}

    multiple = Decimal(str(config.atr_multiple))
    closes = [
        ordered[idx].close
        for idx in window_indices
        if ordered[idx].close is not None
    ]

    for feat in discovered.values():
        key = (feat.detector_id, feat.kind)
        start = window_indices[0] if window_indices else 0
        while start < len(ordered) and ordered[start].ts_available <= feat.ts_available:
            start += 1
        touch = _first_touch(ordered, start, feat.geometry, until_ts=config.to_ts)
        if touch is not None:
            reaction = _measure_reaction(
                ordered,
                atr_values,
                touch,
                feat.geometry,
                horizon_bars=config.horizon_bars,
                atr_multiple=multiple,
            )
            if reaction is None:
                real_skipped[key] += 1
            else:
                real[key].append(reaction)

        follow = _measure_breakout_followthrough(
            ordered,
            atr_values,
            start,
            feat.geometry,
            until_ts=config.to_ts,
            horizon_bars=config.horizon_bars,
            atr_multiple=multiple,
        )
        if follow is not None:
            breakouts[key].append(follow)

    if config.baseline_samples and len(window_indices) >= 4 and closes:
        valid_bars = [
            ordered[idx]
            for idx in window_indices
            if ordered[idx].status is not BarStatus.FEED_GAP and ordered[idx].close is not None
        ]
        for key in ordered_buckets:
            family = buckets[key]
            rng = random.Random(_baseline_seed(config.seed, f"{key[0]}:{key[1]}"))
            for _ in range(config.baseline_samples):
                if family in (StructureFamily.TRENDLINE.value, StructureFamily.CHANNEL.value) and len(valid_bars) >= 4:
                    i1, i2 = sorted(rng.sample(range(len(valid_bars)), 2))
                    b1, b2 = valid_bars[i1], valid_bars[i2]
                    assert b1.close is not None and b2.close is not None
                    geom: StructureGeometry = SlopeLineGeometry(
                        anchor_start=StructureAnchor(ts=b1.timestamp, price=b1.close),
                        anchor_end=StructureAnchor(ts=b2.timestamp, price=b2.close),
                        scale=PriceScaleMode.LINEAR,
                        extend_right=True,
                    )
                    touch_start = window_indices[0]
                else:
                    price = closes[rng.randrange(len(closes))]
                    geom = HorizontalLineGeometry(price=price)
                    touch_start = window_indices[0]

                b_touch = _first_touch(ordered, touch_start, geom, until_ts=config.to_ts)
                if b_touch is None:
                    baseline_skipped[key] += 1
                    continue
                b_react = _measure_reaction(
                    ordered,
                    atr_values,
                    b_touch,
                    geom,
                    horizon_bars=config.horizon_bars,
                    atr_multiple=multiple,
                )
                if b_react is None:
                    baseline_skipped[key] += 1
                    continue
                baseline[key].append(b_react)

    rows: list[DetectorKindEvaluation] = []
    for key in ordered_buckets:
        did, kind = key
        r_list = real[key]
        b_list = baseline[key]
        bo_list = breakouts[key]
        rows.append(
            DetectorKindEvaluation(
                detector_id=did,
                kind=kind,
                family=buckets[key],
                sample_count=len(r_list),
                reactions=sum(1 for r in r_list if r.reacted),
                mean_reaction_atr=(
                    None if not r_list else sum(r.excursion_atr for r in r_list) / len(r_list)
                ),
                breakout_count=len(bo_list),
                breakout_followthroughs=sum(1 for ok in bo_list if ok),
                skipped_count=real_skipped[key],
                baseline_sample_count=len(b_list),
                baseline_reactions=sum(1 for b in b_list if b.reacted),
                baseline_mean_reaction_atr=(
                    None if not b_list else sum(b.excursion_atr for b in b_list) / len(b_list)
                ),
                baseline_skipped_count=baseline_skipped[key],
            )
        )

    churn_rate = None if not churn_samples else sum(churn_samples) / len(churn_samples)
    return StructureEvaluationReport(
        config=config,
        results=tuple(rows),
        churn_rate=churn_rate,
        skipped=sum(real_skipped.values()),
        baseline_skipped=sum(baseline_skipped.values()),
    )


def dumps_structure_report(report: StructureEvaluationReport) -> str:
    return json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=False)
