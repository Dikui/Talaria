"""市场结构共享检测上下文：防前视截断、ATR、缺口分段与多尺度确认 Pivot 缓存。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Sequence

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.derivatives import DerivativePoint
from talaria.foundation.key_levels.contracts import SessionProfile
from talaria.foundation.key_levels.engine import _atr_series, _bar_ms, _normalize
from talaria.foundation.structures.contracts import AnalysisWindow, StructureParams

_MS = 1000
_QUANTUM = Decimal("0.000001")


def make_structure_id(
    instrument_id: str,
    detector_id: str,
    kind: str,
    timeframe: str,
    anchor_ts: int,
    anchor_price: Decimal,
    tag: str = "",
) -> str:
    """生成跨快照稳定的结构唯一标识（``st_`` + 16 位十六进制摘要）。"""

    payload = "|".join(
        (
            instrument_id,
            detector_id,
            kind,
            timeframe,
            str(anchor_ts),
            _normalize(anchor_price),
            tag,
        )
    )
    return "st_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ConfirmedPivot:
    """已过确认期、无前视的摆动高低点锚点。"""

    side: Literal["HIGH", "LOW"]
    price: Decimal
    body_price: Decimal
    index: int
    atr: Decimal | None
    prominence: Decimal
    ts_available: int
    formed_ts: int
    island_id: int
    span: int


def _compute_island_ids(
    visible_with_gaps: list[CompactBar],
    atr_visible: list[Decimal | None],
    island_gap_atr: float,
) -> tuple[int, ...]:
    """为每根非缺失柱分配缺口孤岛编号；遇到 FEED_GAP 或大跳空即递增。"""

    island_ids: list[int] = []
    island_id = 0
    seen_gap = False
    prev_close: Decimal | None = None
    prev_atr: Decimal | None = None
    visible_idx = 0
    gap_mult = Decimal(str(island_gap_atr))

    for bar in visible_with_gaps:
        if bar.status is BarStatus.FEED_GAP:
            if island_ids:
                seen_gap = True
            prev_close = None
            prev_atr = None
            continue
        if seen_gap:
            island_id += 1
            seen_gap = False
        elif (
            prev_close is not None
            and bar.open is not None
            and prev_atr is not None
            and prev_atr > 0
            and abs(bar.open - prev_close) >= gap_mult * prev_atr
        ):
            island_id += 1
        island_ids.append(island_id)
        prev_close = bar.close
        prev_atr = atr_visible[visible_idx]
        visible_idx += 1
    return tuple(island_ids)


@dataclass(slots=True)
class DetectionContext:
    """单次结构检测的只读共享特征上下文，严格截断在 ``cutoff_ts``。"""

    instrument_id: str
    timeframe: str
    cutoff_ts: int
    bar_ms: int
    params: StructureParams
    session_profile: SessionProfile
    visible_with_gaps: tuple[CompactBar, ...]
    visible: tuple[CompactBar, ...]
    atr: tuple[Decimal | None, ...]
    island_ids: tuple[int, ...]
    derivative_points: tuple[DerivativePoint, ...]
    analysis_window: AnalysisWindow | None
    _pivot_cache: dict[tuple[int, int, float], tuple[ConfirmedPivot, ...]] = field(
        default_factory=dict, repr=False
    )

    @classmethod
    def build(
        cls,
        *,
        instrument_id: str,
        timeframe: str,
        bars: Sequence[CompactBar],
        cutoff_ts: int,
        session_profile: SessionProfile | None = None,
        params: StructureParams | None = None,
        derivative_points: Sequence[DerivativePoint] = (),
    ) -> DetectionContext:
        resolved_params = params or StructureParams()
        profile = session_profile or SessionProfile()
        visible_with_gaps = sorted(
            (bar for bar in bars if bar.ts_available <= cutoff_ts),
            key=lambda bar: bar.timestamp,
        )
        visible = [
            bar for bar in visible_with_gaps if bar.status is not BarStatus.FEED_GAP
        ]
        atr_list = _atr_series(visible, resolved_params.atr_period) if visible else []
        islands = (
            _compute_island_ids(visible_with_gaps, atr_list, resolved_params.island_gap_atr)
            if visible
            else ()
        )
        window = (
            AnalysisWindow(
                from_ts=visible[0].timestamp,
                to_ts=visible[-1].timestamp,
            )
            if visible
            else None
        )
        filtered_derivs = tuple(
            point for point in derivative_points if point.ts_available <= cutoff_ts
        )
        return cls(
            instrument_id=instrument_id,
            timeframe=timeframe,
            cutoff_ts=cutoff_ts,
            bar_ms=_bar_ms(timeframe),
            params=resolved_params,
            session_profile=profile,
            visible_with_gaps=tuple(visible_with_gaps),
            visible=tuple(visible),
            atr=tuple(atr_list),
            island_ids=islands,
            derivative_points=filtered_derivs,
            analysis_window=window,
        )

    @property
    def latest_atr(self) -> Decimal:
        """返回最新有效 ATR；预热不足时回退到最近收盘价的 0.5%。"""

        for value in reversed(self.atr):
            if value is not None and value > 0:
                return value
        for bar in reversed(self.visible):
            if bar.close is not None and bar.close > 0:
                return max(bar.close * Decimal("0.005"), _QUANTUM)
        return Decimal("1")

    def bar_atr(self, index: int) -> Decimal:
        value = self.atr[index] if 0 <= index < len(self.atr) else None
        if value is not None and value > 0:
            return value
        bar = self.visible[index]
        if bar.close is not None and bar.close > 0:
            return max(bar.close * Decimal("0.005"), _QUANTUM)
        return self.latest_atr

    def confirmed_pivots(
        self,
        *,
        span: int | None = None,
        confirm_bars: int | None = None,
        pivot_atr_min: float | None = None,
    ) -> tuple[ConfirmedPivot, ...]:
        """提取在 ``cutoff_ts`` 前已完成右侧确认的无前视摆动点。"""

        s = self.params.swing_span if span is None else span
        c = self.params.confirm_bars if confirm_bars is None else confirm_bars
        p_min = self.params.pivot_atr_min if pivot_atr_min is None else pivot_atr_min
        key = (s, c, p_min)
        cached = self._pivot_cache.get(key)
        if cached is not None:
            return cached

        bars = self.visible
        if len(bars) < 2 * s + 1:
            self._pivot_cache[key] = ()
            return ()

        confirm_offset = max(s, c)
        highs = [bar.high for bar in bars]
        lows = [bar.low for bar in bars]
        pivots: list[ConfirmedPivot] = []

        for index in range(s, len(bars) - s):
            confirm_index = index + confirm_offset
            if confirm_index >= len(bars):
                continue
            confirm_bar = bars[confirm_index]
            if confirm_bar.ts_available > self.cutoff_ts:
                continue
            bar = bars[index]
            if (
                bar.high is None
                or bar.low is None
                or bar.open is None
                or bar.close is None
            ):
                continue
            local_atr = self.atr[index]
            threshold = (local_atr or Decimal("0")) * Decimal(str(p_min))
            window_high = max(
                (highs[pos] for pos in range(index - s, index + s + 1) if highs[pos] is not None),
                default=None,
            )
            window_low = min(
                (lows[pos] for pos in range(index - s, index + s + 1) if lows[pos] is not None),
                default=None,
            )
            if window_high is not None and bar.high == window_high:
                neighbours = [
                    highs[pos]
                    for pos in (*range(index - s, index), *range(index + 1, index + s + 1))
                    if highs[pos] is not None
                ]
                if neighbours:
                    prominence = bar.high - max(neighbours)
                    if prominence >= threshold:
                        pivots.append(
                            ConfirmedPivot(
                                side="HIGH",
                                price=bar.high,
                                body_price=max(bar.open, bar.close),
                                index=index,
                                atr=local_atr,
                                prominence=prominence,
                                ts_available=confirm_bar.ts_available,
                                formed_ts=bar.timestamp,
                                island_id=self.island_ids[index],
                                span=s,
                            )
                        )
            if window_low is not None and bar.low == window_low:
                neighbours = [
                    lows[pos]
                    for pos in (*range(index - s, index), *range(index + 1, index + s + 1))
                    if lows[pos] is not None
                ]
                if neighbours:
                    prominence = min(neighbours) - bar.low
                    if prominence >= threshold:
                        pivots.append(
                            ConfirmedPivot(
                                side="LOW",
                                price=bar.low,
                                body_price=min(bar.open, bar.close),
                                index=index,
                                atr=local_atr,
                                prominence=prominence,
                                ts_available=confirm_bar.ts_available,
                                formed_ts=bar.timestamp,
                                island_id=self.island_ids[index],
                                span=s,
                            )
                        )
        result = tuple(pivots)
        self._pivot_cache[key] = result
        return result
