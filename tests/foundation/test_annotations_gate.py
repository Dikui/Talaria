"""智能体标注硬门控：快照/证据/价格/时间四段拒绝码与边界。

纯 Python 单元测试，不经过 HTTP；覆盖任务单 18 要求的每条拒绝码。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from talaria.foundation.annotations import (
    AgentAnnotationCreate,
    AnnotationEvidence,
    AnnotationGateError,
    AnnotationKind,
    evaluate_annotation,
)
from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.snapshot import SnapshotMeta, SnapshotMode
from talaria.foundation.types import PrecisionTier

pytestmark = pytest.mark.small

INSTRUMENT = "BTC-USDT-PERP"
TIMEFRAME = "1m"
BASE_TS = 1_763_596_800_000
MINUTE = 60_000
BAR_COUNT = 20

INSTRUMENT_SPEC = InstrumentSpec(
    instrument_id=INSTRUMENT,
    venue="bybit",
    symbol="BTCUSDT",
    asset_class="crypto_perp",
    tick_size="0.1",
    qty_step="0.001",
    min_qty="0.001",
    price_precision=1,
    qty_precision=3,
)


def _bar(ts: int, *, high: str = "101", low: str = "99", close: str = "100") -> CompactBar:
    return CompactBar(
        timestamp=ts,
        open=close,
        high=high,
        low=low,
        close=close,
        volume="1",
        turnover="100",
        status=BarStatus.NORMAL,
        ts_available=ts + MINUTE,
        precision=PrecisionTier.SECOND_VERIFIED,
    )


def _gap(ts: int) -> CompactBar:
    return CompactBar(
        timestamp=ts,
        open=None,
        high=None,
        low=None,
        close=None,
        volume=None,
        turnover=None,
        status=BarStatus.FEED_GAP,
        ts_available=ts + MINUTE,
        precision=PrecisionTier.SECOND_VERIFIED,
    )


def _bars(count: int = BAR_COUNT) -> list[CompactBar]:
    return [_bar(BASE_TS + index * MINUTE) for index in range(count)]


def _meta(*, cutoff: int | None = None, frames: list[str] | None = None) -> SnapshotMeta:
    return SnapshotMeta(
        snapshot_id="snap_annotation",
        instrument_id=INSTRUMENT,
        timeframes=frames or [TIMEFRAME],
        cutoff_ts=cutoff if cutoff is not None else BASE_TS + BAR_COUNT * MINUTE,
        created_ts=BASE_TS + BAR_COUNT * MINUTE,
        mode=SnapshotMode.HISTORICAL_PIT,
        bar_counts={TIMEFRAME: BAR_COUNT},
    )


def _annotation(**overrides) -> AgentAnnotationCreate:
    values: dict = {
        "snapshot_id": "snap_annotation",
        "instrument_id": INSTRUMENT,
        "timeframe": TIMEFRAME,
        "session_id": "session-1",
        "round": 1,
        "kind": AnnotationKind.ZONE,
        "label": "支撑带",
        "rationale": "前低与成交量密集区重叠",
        "price_low": "100",
        "price_high": "100.5",
        "ts_anchor": BASE_TS,
        "evidence_refs": [AnnotationEvidence(level_id="kl_1")],
    }
    values.update(overrides)
    return AgentAnnotationCreate(**values)


def _evaluate(annotation: AgentAnnotationCreate, **overrides) -> None:
    values = {
        "instrument": INSTRUMENT_SPEC,
        "snapshot_meta": _meta(),
        "bars": _bars(),
        "valid_level_ids": {"kl_1", "kl_2"},
    }
    values.update(overrides)
    evaluate_annotation(annotation, **values)


def _code(annotation: AgentAnnotationCreate, **overrides) -> str:
    with pytest.raises(AnnotationGateError) as caught:
        _evaluate(annotation, **overrides)
    return caught.value.code


def test_valid_annotation_passes() -> None:
    _evaluate(_annotation())
    _evaluate(
        _annotation(
            kind=AnnotationKind.LEVEL,
            price_low="100",
            price_high="100",
        )
    )


def test_snapshot_instrument_mismatch_rejected() -> None:
    meta = _meta()
    meta = meta.model_copy(update={"instrument_id": "ETH-USDT-PERP"})
    assert _code(_annotation(), snapshot_meta=meta) == "SNAPSHOT_MISMATCH"


def test_snapshot_timeframe_not_in_snapshot_rejected() -> None:
    assert (
        _code(_annotation(), snapshot_meta=_meta(frames=["5m"]))
        == "SNAPSHOT_MISMATCH"
    )


def test_missing_evidence_rejected_before_price() -> None:
    annotation = _annotation(evidence_refs=[], price_low="200", price_high="200")
    assert _code(annotation) == "EVIDENCE_EMPTY"


def test_unknown_level_evidence_rejected() -> None:
    annotation = _annotation(
        evidence_refs=[AnnotationEvidence(level_id="kl_missing")]
    )
    assert _code(annotation) == "EVIDENCE_UNRESOLVED"


def test_kline_evidence_accepts_exact_and_one_bar_tolerance() -> None:
    for offset in (-1, 0, 1):
        annotation = _annotation(
            evidence_refs=[
                AnnotationEvidence(kline_ts=BASE_TS + 5 * MINUTE + offset * MINUTE)
            ]
        )
        _evaluate(annotation)


def test_kline_evidence_beyond_one_bar_rejected() -> None:
    annotation = _annotation(
        evidence_refs=[AnnotationEvidence(kline_ts=BASE_TS - 2 * MINUTE)]
    )
    assert _code(annotation) == "EVIDENCE_UNRESOLVED"


def test_non_positive_price_rejected() -> None:
    annotation = _annotation(price_low="0", price_high="100")
    assert _code(annotation) == "PRICE_INVALID"


def test_price_not_tick_aligned_rejected() -> None:
    annotation = _annotation(price_low="100", price_high="100.05")
    assert _code(annotation) == "PRICE_NOT_TICK_ALIGNED"


def test_price_outside_atr_envelope_rejected() -> None:
    # 常量窗口：high=101、low=99、TR=2，ATR_14=2，包络 [93, 107]。
    assert _code(_annotation(price_low="107.1", price_high="107.1")) == (
        "PRICE_OUT_OF_BOUNDS"
    )
    assert _code(_annotation(price_low="92.9", price_high="92.9"),) == (
        "PRICE_OUT_OF_BOUNDS"
    )


def test_atr_envelope_boundaries_are_inclusive() -> None:
    _evaluate(_annotation(price_low="93", price_high="93"))
    _evaluate(_annotation(price_low="107", price_high="107"))


def test_feed_gap_excluded_from_envelope() -> None:
    bars = _bars()
    bars[5] = _gap(BASE_TS + 5 * MINUTE)
    _evaluate(_annotation(), bars=bars)
    assert (
        _code(_annotation(price_low="107.1", price_high="107.1"), bars=bars)
        == "PRICE_OUT_OF_BOUNDS"
    )


def test_uncomputable_atr_rejects_without_envelope() -> None:
    annotation = _annotation()
    assert _code(annotation, bars=_bars(5)) == "PRICE_OUT_OF_BOUNDS"


def test_time_before_snapshot_start_rejected() -> None:
    annotation = _annotation(ts_anchor=BASE_TS - MINUTE)
    assert _code(annotation) == "TIME_BEFORE_SNAPSHOT"


def test_future_horizon_allows_one_hundred_bars() -> None:
    last = _bars()[-1].timestamp
    _evaluate(_annotation(ts_anchor=last + 100 * MINUTE))
    annotation = _annotation(ts_anchor=last + 101 * MINUTE)
    assert _code(annotation) == "TIME_TOO_FAR_FUTURE"


def test_level_kind_requires_single_price() -> None:
    with pytest.raises(ValueError):
        _annotation(
            kind=AnnotationKind.LEVEL,
            price_low="99",
            price_high="101",
        )


def test_tick_size_is_decimal_not_float() -> None:
    assert INSTRUMENT_SPEC.tick_size == Decimal("0.1")
