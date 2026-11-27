"""智能体标注硬门控：纯函数、确定性、由 Python 单侧执行。

前端只负责展示拒绝原因，不重复实现任何价格/时间判定。门控顺序固定为
快照 → 证据 → 价格 → 时间；任一失败抛 :class:`AnnotationGateError`，
调用方不得部分写入。
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from decimal import Decimal

from talaria.foundation.annotations.contracts import AgentAnnotationCreate
from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.errors import InvalidRequestError
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.operators import (
    IndicatorName,
    IndicatorSpec,
    compute_series,
)
from talaria.foundation.snapshot import SnapshotMeta
from talaria.foundation.timeframes import timeframe_ms

SNAPSHOT_MISMATCH = "SNAPSHOT_MISMATCH"
EVIDENCE_EMPTY = "EVIDENCE_EMPTY"
EVIDENCE_UNRESOLVED = "EVIDENCE_UNRESOLVED"
PRICE_INVALID = "PRICE_INVALID"
PRICE_NOT_TICK_ALIGNED = "PRICE_NOT_TICK_ALIGNED"
PRICE_OUT_OF_BOUNDS = "PRICE_OUT_OF_BOUNDS"
TIME_BEFORE_SNAPSHOT = "TIME_BEFORE_SNAPSHOT"
TIME_TOO_FAR_FUTURE = "TIME_TOO_FAR_FUTURE"

#: 价格包络与时间推演的硬边界；跨标的引用一律拒绝。
ATR_PERIOD = 14
ATR_BAND_MULTIPLIER = Decimal("3")
FUTURE_BARS_LIMIT = 100
#: K 线证据允许与真实 bar 相差的根数（±1 根）。
KLINE_EVIDENCE_TOLERANCE_BARS = 1


class AnnotationGateError(InvalidRequestError):
    """结构化拒绝：``code`` 直接进入 HTTP 错误响应。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _reject(code: str, message: str) -> None:
    raise AnnotationGateError(code, message)


def _check_snapshot(
    annotation: AgentAnnotationCreate,
    instrument: InstrumentSpec,
    meta: SnapshotMeta,
) -> None:
    if meta.instrument_id != annotation.instrument_id:
        _reject(
            SNAPSHOT_MISMATCH,
            f"快照属于 {meta.instrument_id}，但标注声明 {annotation.instrument_id}",
        )
    if instrument.instrument_id != annotation.instrument_id:
        _reject(
            SNAPSHOT_MISMATCH,
            f"未知标的 {annotation.instrument_id}",
        )
    if annotation.timeframe not in meta.timeframes:
        _reject(
            SNAPSHOT_MISMATCH,
            f"快照不包含周期 {annotation.timeframe}",
        )


def _check_evidence(
    annotation: AgentAnnotationCreate,
    *,
    bars: Sequence[CompactBar],
    valid_level_ids: set[str],
    valid_structure_ids: set[str] | None = None,
) -> None:
    if not annotation.evidence_refs:
        _reject(EVIDENCE_EMPTY, "智能体标注必须至少引用一条快照证据")
    timestamps = [bar.timestamp for bar in bars]
    tolerance = KLINE_EVIDENCE_TOLERANCE_BARS * timeframe_ms(annotation.timeframe)
    combined_ids = valid_level_ids | (valid_structure_ids or set())
    unresolved: list[str] = []
    for evidence in annotation.evidence_refs:
        if evidence.structure_id is not None:
            if evidence.structure_id not in combined_ids:
                unresolved.append(f"structure_id={evidence.structure_id}")
            continue
        if evidence.level_id is not None:
            if evidence.level_id not in combined_ids:
                unresolved.append(f"level_id={evidence.level_id}")
            continue
        assert evidence.kline_ts is not None
        index = bisect_left(timestamps, evidence.kline_ts)
        candidates = [
            timestamps[position]
            for position in (index - 1, index)
            if 0 <= position < len(timestamps)
        ]
        if not any(
            abs(timestamp - evidence.kline_ts) <= tolerance for timestamp in candidates
        ):
            unresolved.append(f"kline_ts={evidence.kline_ts}")
    if unresolved:
        _reject(
            EVIDENCE_UNRESOLVED,
            "以下证据无法在快照中定位：" + "、".join(unresolved),
        )


def _atr_envelope(
    bars: Sequence[CompactBar],
) -> tuple[Decimal, Decimal] | None:
    """只使用非 FEED_GAP 的窗口 K 线；缺口不参与极值与 ATR。"""

    usable = [bar for bar in bars if bar.status is not BarStatus.FEED_GAP]
    if not usable:
        return None
    highs = [bar.high for bar in usable if bar.high is not None]
    lows = [bar.low for bar in usable if bar.low is not None]
    if not highs or not lows:
        return None
    spec = IndicatorSpec(name=IndicatorName.ATR, period=ATR_PERIOD)
    state = compute_series(usable, spec)
    atr = next(
        (point["value"] for point in reversed(state.points) if point.get("value") is not None),
        None,
    )
    if atr is None:
        return None
    atr = Decimal(atr)
    return (
        min(lows) - ATR_BAND_MULTIPLIER * atr,
        max(highs) + ATR_BAND_MULTIPLIER * atr,
    )


def _check_prices(
    annotation: AgentAnnotationCreate,
    *,
    instrument: InstrumentSpec,
    bars: Sequence[CompactBar],
) -> None:
    low = annotation.price_low
    high = annotation.price_high
    if low <= 0 or high <= 0:
        _reject(PRICE_INVALID, "标注价格必须大于 0")
    tick = instrument.tick_size
    if low % tick != 0 or high % tick != 0:
        _reject(
            PRICE_NOT_TICK_ALIGNED,
            f"价格必须对齐最小变动价位 {tick}",
        )
    envelope = _atr_envelope(bars)
    if envelope is None:
        _reject(PRICE_OUT_OF_BOUNDS, "快照窗口内无法计算 ATR_14，拒绝无界价格")
    lower, upper = envelope
    if low < lower or high > upper:
        _reject(
            PRICE_OUT_OF_BOUNDS,
            f"价格必须落在 [{lower}, {upper}]（窗口极值 ±3×ATR_14）",
        )


def _check_time(
    annotation: AgentAnnotationCreate,
    *,
    bars: Sequence[CompactBar],
) -> None:
    if not bars:
        _reject(SNAPSHOT_MISMATCH, "快照窗口为空")
    snapshot_start = bars[0].timestamp
    if annotation.ts_anchor < snapshot_start:
        _reject(
            TIME_BEFORE_SNAPSHOT,
            f"时间锚点 {annotation.ts_anchor} 早于快照起点 {snapshot_start}",
        )
    horizon = bars[-1].timestamp + FUTURE_BARS_LIMIT * timeframe_ms(annotation.timeframe)
    if annotation.ts_anchor > horizon:
        _reject(
            TIME_TOO_FAR_FUTURE,
            f"时间锚点最多向未来推演 {FUTURE_BARS_LIMIT} 根，最晚 {horizon}",
        )


def evaluate_annotation(
    annotation: AgentAnnotationCreate,
    *,
    instrument: InstrumentSpec,
    snapshot_meta: SnapshotMeta,
    bars: Sequence[CompactBar],
    valid_level_ids: set[str],
    valid_structure_ids: set[str] | None = None,
) -> None:
    """执行全部门控；通过则返回 ``None``，失败抛结构化错误。"""

    _check_snapshot(annotation, instrument, snapshot_meta)
    _check_evidence(
        annotation,
        bars=bars,
        valid_level_ids=valid_level_ids,
        valid_structure_ids=valid_structure_ids,
    )
    _check_prices(annotation, instrument=instrument, bars=bars)
    _check_time(annotation, bars=bars)
