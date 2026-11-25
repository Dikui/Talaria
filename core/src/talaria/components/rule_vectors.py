"""Deterministic cross-language JSON golden vector generator for rules.

Covers the four high-risk rule evaluation boundaries shared between Python
(`talaria.components.rules`) and Rust (`talaria-server::rules`):
1. `cross_above` / `cross_below` across `FEED_GAP`, `NO_TRADES`, and revisions.
2. `overlay_bounds` trendline/channel slope interpolation and decimal rounding.
3. `KeyLevel` `RETEST` state machine (`RuleState.breakouts` / `consumed_sides`).
4. `event`-driven vs `bar_close` evaluation boundary isolation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from talaria.components.rules import RuleObservation, compile_rule
from talaria.components.runtime import Ctx, RuleState
from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.drawings import DrawingKind, DrawingPoint, DrawingRecord, DrawingStyle
from talaria.foundation.feed import DerivativeTickEvent, FeedEvent, KlineEvent, OrderBookEvent
from talaria.foundation.key_levels import (
    EvidenceClass,
    KeyLevel,
    KeyLevelCategory,
    KeyLevelKind,
    KeyLevelStatus,
)
from talaria.foundation.rules import Rule, RuleExpression, RuleGroup, RuleLevelEvent
from talaria.foundation.types import PrecisionTier, decimal_to_string

EPOCH_MS = 1_700_000_000_000
MINUTE_MS = 60_000
INSTRUMENT = "BTC-USDT-PERP"
TIMEFRAME = "1m"

VECTOR_FILENAMES = (
    "cross_gaps_revisions.json",
    "overlay_bounds.json",
    "key_level_retest.json",
    "event_vs_bar_close.json",
)


def default_vector_dir() -> Path:
    """Return the repository `contracts/rule_vectors` directory."""
    return Path(__file__).resolve().parents[4] / "contracts" / "rule_vectors"


def _fmt_dec(value: Decimal | None) -> str | None:
    return None if value is None else decimal_to_string(value)


def _bar(
    ts: int,
    close: str,
    *,
    open: str | None = None,
    high: str | None = None,
    low: str | None = None,
    volume: str = "1",
    turnover: str | None = None,
    status: BarStatus = BarStatus.NORMAL,
    ts_available: int | None = None,
) -> CompactBar:
    o = close if open is None else open
    h = close if high is None else high
    l = close if low is None else low
    t = close if turnover is None else turnover
    return CompactBar(
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(l),
        close=Decimal(close),
        volume=Decimal(volume),
        turnover=Decimal(t),
        status=status,
        ts_available=ts + MINUTE_MS if ts_available is None else ts_available,
        precision=PrecisionTier.SECOND_VERIFIED,
    )


def _no_trades_bar(ts: int, price: str, *, ts_available: int | None = None) -> CompactBar:
    return _bar(
        ts,
        price,
        open=price,
        high=price,
        low=price,
        volume="0",
        turnover="0",
        status=BarStatus.NO_TRADES,
        ts_available=ts_available,
    )


def _gap_bar(ts: int, *, ts_available: int | None = None) -> CompactBar:
    return CompactBar(
        timestamp=ts,
        open=None,
        high=None,
        low=None,
        close=None,
        volume=None,
        turnover=None,
        status=BarStatus.FEED_GAP,
        ts_available=ts + MINUTE_MS if ts_available is None else ts_available,
        precision=PrecisionTier.SECOND_VERIFIED,
    )


def _drawing(
    drawing_id: str,
    kind: DrawingKind,
    points: list[tuple[int, str]],
    *,
    updated_ts: int = 0,
    deleted_at: int | None = None,
) -> DrawingRecord:
    return DrawingRecord(
        drawing_id=drawing_id,
        instrument_id=INSTRUMENT,
        kind=kind,
        visible_timeframes=[],
        points=[DrawingPoint(ts=ts, price=Decimal(price)) for ts, price in points],
        text="",
        style=DrawingStyle(),
        version=1,
        created_ts=0,
        updated_ts=updated_ts,
        deleted_at=deleted_at,
    )


def _level(
    level_id: str,
    *,
    status: KeyLevelStatus = KeyLevelStatus.BROKEN,
    price: str | None = None,
    low: str | None = None,
    high: str | None = None,
    kind: KeyLevelKind = KeyLevelKind.PREV_DAY_HIGH,
    ts_available: int = 0,
) -> KeyLevel:
    return KeyLevel(
        level_id=level_id,
        instrument_id=INSTRUMENT,
        kind=kind,
        category=KeyLevelCategory.SESSION,
        evidence_class=EvidenceClass.OBSERVED,
        timeframe=TIMEFRAME,
        price=Decimal(price) if price is not None else None,
        price_low=Decimal(low) if low is not None else None,
        price_high=Decimal(high) if high is not None else None,
        strength=0.8,
        status=status,
        ts_available=ts_available,
        formed_ts=ts_available,
        touch_count=0,
        volume_weight=Decimal("1"),
    )


def _serialize_event(event: FeedEvent | None) -> dict[str, Any] | None:
    if event is None:
        return None
    if isinstance(event, KlineEvent):
        return {"kind": "kline", "payload": event.model_dump(mode="json")}
    if isinstance(event, OrderBookEvent):
        return {"kind": "order_book", "payload": event.model_dump(mode="json")}
    if isinstance(event, DerivativeTickEvent):
        return {"kind": "derivative_tick", "payload": event.model_dump(mode="json")}
    raise TypeError(f"Unsupported rule vector event type: {type(event)!r}")


def _serialize_observation(obs: RuleObservation | None) -> dict[str, Any] | None:
    if obs is None:
        return None
    return {
        "matched": obs.matched,
        "observed": _fmt_dec(obs.observed),
        "threshold": _fmt_dec(obs.threshold),
        "drawing_kind": obs.drawing_kind.value if obs.drawing_kind is not None else None,
        "level_id": obs.level.level_id if obs.level is not None else None,
        "level_event": obs.level_event.value if obs.level_event is not None else None,
        "level_direction": obs.level_direction,
        "crossed_at": obs.crossed_at,
    }


def _retest_node_paths(expression: RuleExpression, path: str = "root") -> dict[int, str]:
    """Map Python `id(node)` for `RuleLevelEvent` nodes to structural AST paths matching Rust."""
    paths: dict[int, str] = {}
    if isinstance(expression, RuleGroup):
        for idx, child in enumerate(expression.args):
            paths.update(_retest_node_paths(child, f"{path}.{idx}"))
    elif isinstance(expression, RuleLevelEvent):
        paths[id(expression)] = path
    return paths


def serialize_rule_state(state: RuleState, node_paths: dict[int, str]) -> dict[str, Any]:
    """Serialize `RuleState.breakouts` and `consumed_sides` using structural AST paths."""
    breakouts = {
        node_paths[key]: [direction, ts]
        for key, (direction, ts) in state.breakouts.items()
        if key in node_paths
    }
    consumed_sides = {
        node_paths[key]: direction
        for key, direction in state.consumed_sides.items()
        if key in node_paths
    }
    return {
        "breakouts": dict(sorted(breakouts.items())),
        "consumed_sides": dict(sorted(consumed_sides.items())),
    }


@dataclass(frozen=True, slots=True)
class _StepSpec:
    now_ms: int
    upsert_bars: tuple[CompactBar, ...] = ()
    bar_ts: int | None = None
    event: FeedEvent | None = None
    levels: tuple[KeyLevel, ...] | None = None
    consume_retests: bool = True
    record_state: bool = False


@dataclass(frozen=True, slots=True)
class _CaseSpec:
    name: str
    description: str
    rule: dict[str, Any]
    steps: tuple[_StepSpec, ...]
    drawings: tuple[DrawingRecord, ...] = ()
    levels: tuple[KeyLevel, ...] = ()
    derivatives: tuple[DerivativePoint, ...] = ()
    start_ts: int = EPOCH_MS


def _build_case(spec: _CaseSpec) -> dict[str, Any]:
    rule = Rule.model_validate(spec.rule)
    compiled = compile_rule(rule)
    node_paths = _retest_node_paths(rule.expression)
    drawing_map = {d.drawing_id: d for d in spec.drawings}
    default_levels = {lv.level_id: lv for lv in spec.levels}
    deriv_by_kind: dict[DerivativeKind, list[DerivativePoint]] = {}
    for pt in spec.derivatives:
        deriv_by_kind.setdefault(pt.kind, []).append(pt)

    bars_by_ts: dict[int, CompactBar] = {}
    state = RuleState()
    serialized_steps: list[dict[str, Any]] = []

    for step in spec.steps:
        for bar in step.upsert_bars:
            bars_by_ts[bar.timestamp] = bar
        confirmed = tuple(bars_by_ts[ts] for ts in sorted(bars_by_ts))
        step_levels = (
            default_levels
            if step.levels is None
            else {lv.level_id: lv for lv in step.levels}
        )
        ctx = Ctx(
            INSTRUMENT,
            TIMEFRAME,
            spec.start_ts,
            step.now_ms,
            _confirmed_compact=confirmed,
            _bar_ts=step.bar_ts,
            _indicator_defaults={},
            _read_derivative_revisions=lambda kind: tuple(deriv_by_kind.get(kind, ())),
            drawing=lambda drawing_id: drawing_map.get(drawing_id),
            levels=step_levels,
            rule_state=state,
        )
        obs = compiled.observe(
            ctx,
            event=step.event,
            state=state,
            consume_retests=step.consume_retests,
        )
        step_dict: dict[str, Any] = {
            "now_ms": step.now_ms,
            "bar_ts": step.bar_ts,
            "consume_retests": step.consume_retests,
            "upsert_bars": [bar.model_dump(mode="json") for bar in step.upsert_bars],
            "event": _serialize_event(step.event),
            "expected": _serialize_observation(obs),
        }
        if step.levels is not None:
            step_dict["levels"] = [lv.model_dump(mode="json") for lv in step.levels]
        if step.record_state:
            step_dict["expected_state"] = serialize_rule_state(state, node_paths)
        serialized_steps.append(step_dict)

    return {
        "name": spec.name,
        "description": spec.description,
        "start_ts": spec.start_ts,
        "rule": rule.model_dump(mode="json", exclude_unset=True),
        "drawings": [d.model_dump(mode="json") for d in spec.drawings],
        "levels": [lv.model_dump(mode="json") for lv in spec.levels],
        "derivatives": [pt.model_dump(mode="json") for pt in spec.derivatives],
        "steps": serialized_steps,
    }


def _cross_gaps_revisions_suite() -> dict[str, Any]:
    t0 = EPOCH_MS
    t1 = EPOCH_MS + MINUTE_MS
    t2 = EPOCH_MS + 2 * MINUTE_MS
    t3 = EPOCH_MS + 3 * MINUTE_MS
    t4 = EPOCH_MS + 4 * MINUTE_MS
    t5 = EPOCH_MS + 5 * MINUTE_MS

    cases = [
        _CaseSpec(
            name="cross_above_constant_across_feed_gap",
            description="FEED_GAP must yield None and prevent fabricating a cross on the next NORMAL bar.",
            rule={
                "version": "1",
                "timing": "bar_close",
                "expression": {
                    "op": "compare",
                    "test": "cross_above",
                    "left": {"source": "bar", "field": "close"},
                    "right": {"source": "constant", "value": "105"},
                },
            },
            steps=(
                _StepSpec(now_ms=t1, bar_ts=t0, upsert_bars=(_bar(t0, "100"),)),
                _StepSpec(now_ms=t2, bar_ts=t1, upsert_bars=(_gap_bar(t1),)),
                _StepSpec(now_ms=t3, bar_ts=t2, upsert_bars=(_bar(t2, "110"),)),
                _StepSpec(now_ms=t4, bar_ts=t3, upsert_bars=(_bar(t3, "112"),)),
                _StepSpec(now_ms=t5, bar_ts=t4, upsert_bars=(_bar(t4, "102"),)),
                _StepSpec(now_ms=t5 + MINUTE_MS, bar_ts=t5, upsert_bars=(_bar(t5, "108"),)),
            ),
        ),
        _CaseSpec(
            name="cross_below_constant_across_no_trades",
            description="NO_TRADES preserves previous close as valid prior bar for cross_below.",
            rule={
                "version": "1",
                "timing": "bar_close",
                "expression": {
                    "op": "compare",
                    "test": "cross_below",
                    "left": {"source": "bar", "field": "close"},
                    "right": {"source": "constant", "value": "100"},
                },
            },
            steps=(
                _StepSpec(now_ms=t1, bar_ts=t0, upsert_bars=(_bar(t0, "102"),)),
                _StepSpec(now_ms=t2, bar_ts=t1, upsert_bars=(_no_trades_bar(t1, "102"),)),
                _StepSpec(now_ms=t3, bar_ts=t2, upsert_bars=(_bar(t2, "99"),)),
                _StepSpec(now_ms=t4, bar_ts=t3, upsert_bars=(_no_trades_bar(t3, "99"),)),
            ),
        ),
        _CaseSpec(
            name="cross_above_ema_across_feed_gap_and_no_trades",
            description="EMA resets across FEED_GAP and sustains state across NO_TRADES before crossing.",
            rule={
                "version": "1",
                "timing": "bar_close",
                "expression": {
                    "op": "compare",
                    "test": "cross_above",
                    "left": {"source": "bar", "field": "close"},
                    "right": {"source": "indicator", "name": "EMA", "params": {"period": 3}},
                },
            },
            steps=(
                _StepSpec(now_ms=t1, bar_ts=t0, upsert_bars=(_bar(t0, "100"),)),
                _StepSpec(now_ms=t2, bar_ts=t1, upsert_bars=(_no_trades_bar(t1, "100"),)),
                _StepSpec(now_ms=t3, bar_ts=t2, upsert_bars=(_bar(t2, "106"),)),
                _StepSpec(now_ms=t4, bar_ts=t3, upsert_bars=(_gap_bar(t3),)),
                _StepSpec(now_ms=t5, bar_ts=t4, upsert_bars=(_bar(t4, "110"),)),
                _StepSpec(now_ms=t5 + MINUTE_MS, bar_ts=t5, upsert_bars=(_bar(t5, "114"),)),
            ),
        ),
        _CaseSpec(
            name="cross_above_bar_revision_flips_signal",
            description="Confirmed bar revision updating close above threshold triggers cross on revised bar and suppresses redundant cross on next bar.",
            rule={
                "version": "1",
                "timing": "bar_close",
                "expression": {
                    "op": "compare",
                    "test": "cross_above",
                    "left": {"source": "bar", "field": "close"},
                    "right": {"source": "constant", "value": "105"},
                },
            },
            steps=(
                _StepSpec(now_ms=t1, bar_ts=t0, upsert_bars=(_bar(t0, "100"),)),
                _StepSpec(now_ms=t2, bar_ts=t1, upsert_bars=(_bar(t1, "104", ts_available=t2),)),
                _StepSpec(
                    now_ms=t2 + 15_000,
                    bar_ts=t1,
                    upsert_bars=(_bar(t1, "106", ts_available=t2 + 15_000),),
                    event=KlineEvent(
                        instrument_id=INSTRUMENT,
                        timeframe=TIMEFRAME,
                        bar=_bar(t1, "106", ts_available=t2 + 15_000),
                        confirmed=True,
                        revision=1,
                    ),
                ),
                _StepSpec(now_ms=t3, bar_ts=t2, upsert_bars=(_bar(t2, "108"),)),
            ),
        ),
        _CaseSpec(
            name="cross_below_sma_implicit_time_gap",
            description="Non-contiguous bar timestamp acts as a gap even without an explicit FEED_GAP row.",
            rule={
                "version": "1",
                "timing": "bar_close",
                "expression": {
                    "op": "compare",
                    "test": "cross_below",
                    "left": {"source": "bar", "field": "close"},
                    "right": {"source": "indicator", "name": "SMA", "params": {"period": 2}},
                },
            },
            steps=(
                _StepSpec(now_ms=t1, bar_ts=t0, upsert_bars=(_bar(t0, "100"),)),
                _StepSpec(now_ms=t3, bar_ts=t2, upsert_bars=(_bar(t2, "95"),)),
                _StepSpec(now_ms=t4, bar_ts=t3, upsert_bars=(_bar(t3, "90"),)),
            ),
        ),
    ]
    return {
        "schema_version": 1,
        "category": "cross_above_cross_below_gaps_no_trades_revisions",
        "instrument_id": INSTRUMENT,
        "timeframe": TIMEFRAME,
        "cases": [_build_case(case) for case in cases],
    }


def _overlay_bounds_suite() -> dict[str, Any]:
    t0 = EPOCH_MS
    t1 = EPOCH_MS + MINUTE_MS
    t2 = EPOCH_MS + 2 * MINUTE_MS
    t3 = EPOCH_MS + 3 * MINUTE_MS
    t4 = EPOCH_MS + 4 * MINUTE_MS
    t7 = EPOCH_MS + 7 * MINUTE_MS

    seg_exact = _drawing("seg_exact", DrawingKind.SEGMENT, [(t0, "100"), (t2, "102")])
    ray_third = _drawing("ray_third", DrawingKind.RAY, [(t0, "100"), (t3, "101")])
    chan_seventh = _drawing(
        "chan_seventh",
        DrawingKind.PARALLEL_CHANNEL,
        [(t0, "80000"), (t7, "80001"), (t3, "80100.5")],
    )
    fib_prec = _drawing(
        "fib_prec",
        DrawingKind.FIB_RETRACEMENT,
        [(t0, "100.123456789012345678901234"), (t2, "200.987654321098765432109876")],
    )
    deleted_line = _drawing("deleted_line", DrawingKind.HORIZONTAL_LINE, [(t0, "100")], deleted_at=t1)
    future_line = _drawing("future_line", DrawingKind.HORIZONTAL_LINE, [(t0, "100")], updated_ts=t3)

    cases = [
        _CaseSpec(
            name="segment_exact_midpoint_touch_and_out_of_range",
            description="Exact midpoint interpolation on SEGMENT and None outside [t0, t2].",
            rule={
                "version": "1",
                "timing": "bar_close",
                "expression": {
                    "op": "overlay",
                    "drawing_id": "seg_exact",
                    "mode": "TOUCH",
                    "direction": "ABOVE",
                },
            },
            drawings=(seg_exact,),
            steps=(
                _StepSpec(now_ms=t1, bar_ts=t0, upsert_bars=(_bar(t0, "99", high="100"),)),
                _StepSpec(now_ms=t2, bar_ts=t1, upsert_bars=(_bar(t1, "100.5", high="101"),)),
                _StepSpec(now_ms=t3, bar_ts=t2, upsert_bars=(_bar(t2, "101", high="101.9"),)),
                _StepSpec(now_ms=t4, bar_ts=t3, upsert_bars=(_bar(t3, "105", high="106"),)),
            ),
        ),
        _CaseSpec(
            name="ray_repeating_one_third_slope_extrapolation",
            description="RAY 1/3 slope interpolation & forward extrapolation tests prec=28 vs rust_decimal rounding.",
            rule={
                "version": "1",
                "timing": "bar_close",
                "expression": {
                    "op": "overlay",
                    "drawing_id": "ray_third",
                    "mode": "CLOSE_BREAK",
                    "direction": "ABOVE",
                },
            },
            drawings=(ray_third,),
            steps=(
                _StepSpec(now_ms=t1, bar_ts=t0, upsert_bars=(_bar(t0, "100"),)),
                _StepSpec(now_ms=t2, bar_ts=t1, upsert_bars=(_bar(t1, "100.3333333333333333333333334"),)),
                _StepSpec(now_ms=t3, bar_ts=t2, upsert_bars=(_bar(t2, "100.6666666666666666666666666"),)),
                _StepSpec(now_ms=t4 + MINUTE_MS, bar_ts=t4, upsert_bars=(_bar(t4, "101.4"),)),
            ),
        ),
        _CaseSpec(
            name="parallel_channel_one_seventh_slope_and_offset",
            description="PARALLEL_CHANNEL with 1/7 slope at BTC 80000 scale using both upper and lower bounds.",
            rule={
                "version": "1",
                "timing": "bar_close",
                "expression": {
                    "op": "all",
                    "args": [
                        {
                            "op": "compare",
                            "test": "gt",
                            "left": {"source": "bar", "field": "close"},
                            "right": {"source": "drawing_price", "drawing_id": "chan_seventh", "edge": "lower"},
                        },
                        {
                            "op": "overlay",
                            "drawing_id": "chan_seventh",
                            "mode": "TOUCH",
                            "direction": "ABOVE",
                        },
                    ],
                },
            },
            drawings=(chan_seventh,),
            steps=(
                _StepSpec(now_ms=t2, bar_ts=t1, upsert_bars=(_bar(t1, "80050", high="80120"),)),
                _StepSpec(now_ms=t3, bar_ts=t2, upsert_bars=(_bar(t2, "80050", high="80090"),)),
            ),
        ),
        _CaseSpec(
            name="fib_retracement_high_precision_ratio",
            description="FIB_RETRACEMENT with 24 fractional digits and ratio=0.618.",
            rule={
                "version": "1",
                "timing": "bar_close",
                "expression": {
                    "op": "overlay",
                    "drawing_id": "fib_prec",
                    "mode": "CLOSE_BREAK",
                    "direction": "ABOVE",
                    "ratio": "0.618",
                },
            },
            drawings=(fib_prec,),
            steps=(
                _StepSpec(now_ms=t2, bar_ts=t1, upsert_bars=(_bar(t1, "162.5"),)),
            ),
        ),
        _CaseSpec(
            name="drawing_deleted_and_future_updated_ts_invisible",
            description="Soft-deleted drawings and drawings with updated_ts > now return None.",
            rule={
                "version": "1",
                "timing": "bar_close",
                "expression": {
                    "op": "any",
                    "args": [
                        {
                            "op": "overlay",
                            "drawing_id": "deleted_line",
                            "mode": "TOUCH",
                            "direction": "ABOVE",
                        },
                        {
                            "op": "overlay",
                            "drawing_id": "future_line",
                            "mode": "TOUCH",
                            "direction": "ABOVE",
                        },
                    ],
                },
            },
            drawings=(deleted_line, future_line),
            steps=(
                _StepSpec(now_ms=t2, bar_ts=t1, upsert_bars=(_bar(t1, "105", high="110"),)),
                _StepSpec(now_ms=t4, bar_ts=t3, upsert_bars=(_bar(t3, "105", high="110"),)),
            ),
        ),
    ]
    return {
        "schema_version": 1,
        "category": "overlay_bounds_interpolation_and_rounding",
        "instrument_id": INSTRUMENT,
        "timeframe": TIMEFRAME,
        "cases": [_build_case(case) for case in cases],
    }


def _key_level_retest_suite() -> dict[str, Any]:
    t0 = EPOCH_MS
    t1 = EPOCH_MS + MINUTE_MS
    t2 = EPOCH_MS + 2 * MINUTE_MS
    t3 = EPOCH_MS + 3 * MINUTE_MS
    t4 = EPOCH_MS + 4 * MINUTE_MS
    t5 = EPOCH_MS + 5 * MINUTE_MS

    zone_broken = _level("kl_zone", status=KeyLevelStatus.BROKEN, low="100", high="102")
    zone_active = _level("kl_zone", status=KeyLevelStatus.ACTIVE, low="100", high="102")

    cases = [
        _CaseSpec(
            name="retest_multi_bar_consumption_and_zone_reset",
            description="Arms UP breakout, consumes once on retest, suppresses subsequent UP retest, resets inside zone, and re-arms on next UP breakout.",
            rule={
                "version": "1",
                "timing": "bar_close",
                "expression": {
                    "op": "level_event",
                    "level_id": "kl_zone",
                    "event": "RETEST",
                },
            },
            levels=(zone_broken,),
            steps=(
                _StepSpec(now_ms=t1, bar_ts=t0, upsert_bars=(_bar(t0, "105", low="103", high="106"),), record_state=True),
                _StepSpec(now_ms=t2, bar_ts=t1, upsert_bars=(_bar(t1, "104", low="101", high="105"),), record_state=True),
                _StepSpec(now_ms=t3, bar_ts=t2, upsert_bars=(_bar(t2, "103", low="100.5", high="104"),), record_state=True),
                _StepSpec(now_ms=t4, bar_ts=t3, upsert_bars=(_bar(t3, "101", low="100", high="102"),), record_state=True),
                _StepSpec(now_ms=t5, bar_ts=t4, upsert_bars=(_bar(t4, "104", low="102.5", high="105"),), record_state=True),
                _StepSpec(now_ms=t5 + MINUTE_MS, bar_ts=t5, upsert_bars=(_bar(t5, "103", low="102", high="104"),), record_state=True),
            ),
        ),
        _CaseSpec(
            name="retest_direct_side_flip_up_to_down",
            description="Consumed UP side is cleared and immediately replaced when close flips below lower bound.",
            rule={
                "version": "1",
                "timing": "bar_close",
                "expression": {
                    "op": "level_event",
                    "level_id": "kl_zone",
                    "event": "RETEST",
                },
            },
            levels=(zone_broken,),
            steps=(
                _StepSpec(now_ms=t1, bar_ts=t0, upsert_bars=(_bar(t0, "105", low="103", high="106"),), record_state=True),
                _StepSpec(now_ms=t2, bar_ts=t1, upsert_bars=(_bar(t1, "104", low="101", high="105"),), record_state=True),
                _StepSpec(now_ms=t3, bar_ts=t2, upsert_bars=(_bar(t2, "98", low="97", high="101"),), record_state=True),
                _StepSpec(now_ms=t4, bar_ts=t3, upsert_bars=(_bar(t3, "99", low="98", high="100.5"),), record_state=True),
            ),
        ),
        _CaseSpec(
            name="retest_not_consumed_when_parent_all_fails",
            description="A local RETEST match must NOT be consumed into consumed_sides if the parent ALL expression fails.",
            rule={
                "version": "1",
                "timing": "bar_close",
                "expression": {
                    "op": "all",
                    "args": [
                        {
                            "op": "level_event",
                            "level_id": "kl_zone",
                            "event": "RETEST",
                        },
                        {
                            "op": "compare",
                            "test": "gt",
                            "left": {"source": "bar", "field": "close"},
                            "right": {"source": "constant", "value": "105"},
                        },
                    ],
                },
            },
            levels=(zone_broken,),
            steps=(
                _StepSpec(now_ms=t1, bar_ts=t0, upsert_bars=(_bar(t0, "104", low="103", high="105"),), record_state=True),
                _StepSpec(now_ms=t2, bar_ts=t1, upsert_bars=(_bar(t1, "103", low="101", high="104"),), record_state=True),
                _StepSpec(now_ms=t3, bar_ts=t2, upsert_bars=(_bar(t2, "106", low="101.5", high="107"),), record_state=True),
            ),
        ),
        _CaseSpec(
            name="active_level_defers_retest_until_broken",
            description="While KeyLevel.status is ACTIVE, closes outside bounds yield None without arming RETEST.",
            rule={
                "version": "1",
                "timing": "bar_close",
                "expression": {
                    "op": "level_event",
                    "level_id": "kl_zone",
                    "event": "RETEST",
                },
            },
            levels=(zone_active,),
            steps=(
                _StepSpec(now_ms=t1, bar_ts=t0, upsert_bars=(_bar(t0, "105", low="103", high="106"),), levels=(zone_active,), record_state=True),
                _StepSpec(now_ms=t2, bar_ts=t1, upsert_bars=(_bar(t1, "104", low="101", high="105"),), levels=(zone_broken,), record_state=True),
                _StepSpec(now_ms=t3, bar_ts=t2, upsert_bars=(_bar(t2, "103", low="101", high="104"),), levels=(zone_broken,), record_state=True),
            ),
        ),
    ]
    return {
        "schema_version": 1,
        "category": "key_level_retest_state_machine",
        "instrument_id": INSTRUMENT,
        "timeframe": TIMEFRAME,
        "cases": [_build_case(case) for case in cases],
    }


def _event_vs_bar_close_suite() -> dict[str, Any]:
    t0 = EPOCH_MS
    t1 = EPOCH_MS + MINUTE_MS
    t2 = EPOCH_MS + 2 * MINUTE_MS

    seg = _drawing("seg_1", DrawingKind.SEGMENT, [(t0, "150"), (t2, "150")])
    cases = [
        _CaseSpec(
            name="bar_close_rejects_non_confirmed_events",
            description="bar_close rule returns None on OrderBookEvent, DerivativeTickEvent, and unconfirmed KlineEvent, evaluating only on confirmed close.",
            rule={
                "version": "1",
                "timing": "bar_close",
                "expression": {
                    "op": "compare",
                    "test": "gt",
                    "left": {"source": "bar", "field": "close"},
                    "right": {"source": "constant", "value": "100"},
                },
            },
            steps=(
                _StepSpec(
                    now_ms=t0 + 10_000,
                    upsert_bars=(_bar(t0, "150"),),
                    event=OrderBookEvent(
                        instrument_id=INSTRUMENT,
                        bid_price=Decimal("150"),
                        bid_size=Decimal("1"),
                        ask_price=Decimal("151"),
                        ask_size=Decimal("1"),
                        ts_event=t0 + 10_000,
                        ts_ingested=t0 + 10_000,
                    ),
                ),
                _StepSpec(
                    now_ms=t0 + 20_000,
                    event=DerivativeTickEvent(
                        instrument_id=INSTRUMENT,
                        mark_price=Decimal("150"),
                        ts_event=t0 + 20_000,
                        ts_ingested=t0 + 20_000,
                    ),
                ),
                _StepSpec(
                    now_ms=t0 + 30_000,
                    event=KlineEvent(
                        instrument_id=INSTRUMENT,
                        timeframe=TIMEFRAME,
                        bar=_bar(t0, "150", ts_available=t0 + 30_000),
                        confirmed=False,
                        revision=0,
                    ),
                ),
                _StepSpec(
                    now_ms=t1,
                    bar_ts=t0,
                    event=KlineEvent(
                        instrument_id=INSTRUMENT,
                        timeframe=TIMEFRAME,
                        bar=_bar(t0, "150", ts_available=t1),
                        confirmed=True,
                        revision=0,
                    ),
                ),
            ),
        ),
        _CaseSpec(
            name="event_orderbook_uses_confirmed_indicator_and_visibility_guard",
            description="event rule requires an event, reads only confirmed indicator bars up to now, and rejects future ts_ingested events.",
            rule={
                "version": "1",
                "timing": "event",
                "expression": {
                    "op": "compare",
                    "test": "gt",
                    "left": {"source": "event", "metric": "bid_price"},
                    "right": {"source": "indicator", "name": "EMA", "params": {"period": 1}},
                },
            },
            steps=(
                _StepSpec(
                    now_ms=t1 + 10_000,
                    upsert_bars=(_bar(t0, "100"), _bar(t1, "200")),
                    event=None,
                ),
                _StepSpec(
                    now_ms=t1 + 10_000,
                    event=OrderBookEvent(
                        instrument_id=INSTRUMENT,
                        bid_price=Decimal("120"),
                        bid_size=Decimal("1"),
                        ask_price=Decimal("121"),
                        ask_size=Decimal("1"),
                        ts_event=t1 + 10_000,
                        ts_ingested=t1 + 10_000,
                    ),
                ),
                _StepSpec(
                    now_ms=t1 + 10_000,
                    event=OrderBookEvent(
                        instrument_id=INSTRUMENT,
                        bid_price=Decimal("120"),
                        bid_size=Decimal("1"),
                        ask_price=Decimal("121"),
                        ask_size=Decimal("1"),
                        ts_event=t1 + 10_000,
                        ts_ingested=t1 + 10_001,
                    ),
                ),
            ),
        ),
        _CaseSpec(
            name="event_unconfirmed_kline_caps_indicator_history_before_open_slot",
            description="Unconfirmed KlineEvent at t1 caps SMA history at t0 (<= t1 - tf_ms) while reading LAST_PRICE from the open update.",
            rule={
                "version": "1",
                "timing": "event",
                "expression": {
                    "op": "compare",
                    "test": "gt",
                    "left": {"source": "event", "metric": "last_price"},
                    "right": {"source": "indicator", "name": "SMA", "params": {"period": 1}},
                },
            },
            steps=(
                _StepSpec(
                    now_ms=t2 + MINUTE_MS,
                    upsert_bars=(_bar(t0, "100"), _bar(t1, "200")),
                    event=KlineEvent(
                        instrument_id=INSTRUMENT,
                        timeframe=TIMEFRAME,
                        bar=_bar(t1, "110", ts_available=t2),
                        confirmed=False,
                        revision=1,
                    ),
                ),
            ),
        ),
        _CaseSpec(
            name="event_any_with_orderbook_vs_unconfirmed_kline_bar_nodes",
            description="OrderBookEvent leaves bar-dependent overlay nodes as None (unknown), whereas unconfirmed KlineEvent evaluates them against kline.bar.",
            rule={
                "version": "1",
                "timing": "event",
                "expression": {
                    "op": "any",
                    "args": [
                        {
                            "op": "compare",
                            "test": "gt",
                            "left": {"source": "event", "metric": "bid_price"},
                            "right": {"source": "constant", "value": "150"},
                        },
                        {
                            "op": "overlay",
                            "drawing_id": "seg_1",
                            "mode": "TOUCH",
                            "direction": "ABOVE",
                        },
                    ],
                },
            },
            drawings=(seg,),
            steps=(
                _StepSpec(
                    now_ms=t1 + 10_000,
                    upsert_bars=(_bar(t0, "100"),),
                    event=OrderBookEvent(
                        instrument_id=INSTRUMENT,
                        bid_price=Decimal("140"),
                        bid_size=Decimal("1"),
                        ask_price=Decimal("141"),
                        ask_size=Decimal("1"),
                        ts_event=t1 + 10_000,
                        ts_ingested=t1 + 10_000,
                    ),
                ),
                _StepSpec(
                    now_ms=t1 + 10_000,
                    event=OrderBookEvent(
                        instrument_id=INSTRUMENT,
                        bid_price=Decimal("160"),
                        bid_size=Decimal("1"),
                        ask_price=Decimal("161"),
                        ask_size=Decimal("1"),
                        ts_event=t1 + 10_000,
                        ts_ingested=t1 + 10_000,
                    ),
                ),
                _StepSpec(
                    now_ms=t1 + 20_000,
                    event=KlineEvent(
                        instrument_id=INSTRUMENT,
                        timeframe=TIMEFRAME,
                        bar=_bar(t1, "140", high="155", ts_available=t1 + 20_000),
                        confirmed=False,
                        revision=0,
                    ),
                ),
            ),
        ),
    ]
    return {
        "schema_version": 1,
        "category": "event_vs_bar_close_evaluation_boundary",
        "instrument_id": INSTRUMENT,
        "timeframe": TIMEFRAME,
        "cases": [_build_case(case) for case in cases],
    }


def render_rule_vectors() -> dict[str, str]:
    """Deterministically render all rule golden vector JSON payloads keyed by filename."""
    suites = {
        "cross_gaps_revisions.json": _cross_gaps_revisions_suite(),
        "overlay_bounds.json": _overlay_bounds_suite(),
        "key_level_retest.json": _key_level_retest_suite(),
        "event_vs_bar_close.json": _event_vs_bar_close_suite(),
    }
    return {
        filename: json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        for filename, payload in suites.items()
    }


def write_rule_vectors(directory: Path | None = None) -> list[Path]:
    """Write all deterministic rule golden vectors to `contracts/rule_vectors/`."""
    target_dir = default_vector_dir() if directory is None else directory
    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, content in render_rule_vectors().items():
        path = target_dir / filename
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for path in write_rule_vectors():
        print(path)
