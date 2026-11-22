"""离线覆盖五类数值条件、冷却、断流跳过与快照冻结。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.feed import (
    DerivativeTickEvent,
    FeedHealthEvent,
    FeedHealthStatus,
    KlineEvent,
)
from talaria.foundation.key_levels import (
    EvidenceClass,
    KeyLevel,
    KeyLevelCategory,
    KeyLevelKind,
    KeyLevelStatus,
)
from talaria.foundation.scanner import (
    ScannerConditionKind,
    ScannerTriggerRoute,
)
from talaria.foundation.types import PrecisionTier
from talaria.perception.scanner import ScannerConfig, ScannerEngine

pytestmark = pytest.mark.medium

MINUTE = 60_000
BASE_TS = 1_795_000_000_000


def _bar(
    ts: int,
    *,
    close: float = 100.0,
    high: float | None = None,
    low: float | None = None,
    volume: float = 10.0,
    turnover: float | None = None,
    status: BarStatus = BarStatus.NORMAL,
) -> CompactBar:
    if status is BarStatus.FEED_GAP:
        return CompactBar(
            timestamp=ts,
            open=None,
            high=None,
            low=None,
            close=None,
            volume=None,
            turnover=None,
            status=status,
            ts_available=ts + MINUTE,
            precision=PrecisionTier.SECOND_VERIFIED,
        )
    return CompactBar(
        timestamp=ts,
        open=Decimal(str(close)),
        high=Decimal(str(high if high is not None else close + 0.5)),
        low=Decimal(str(low if low is not None else close - 0.5)),
        close=Decimal(str(close)),
        volume=Decimal(str(volume)),
        turnover=Decimal(str(turnover if turnover is not None else volume * 100)),
        status=status,
        ts_available=ts + MINUTE,
        precision=PrecisionTier.SECOND_VERIFIED,
    )


def _kline(bar: CompactBar, *, instrument: str = "BTC-USDT-PERP") -> KlineEvent:
    return KlineEvent(
        instrument_id=instrument,
        timeframe="1m",
        bar=bar,
        confirmed=True,
        revision=0,
    )


def _funding_point(ts: int, value: float) -> DerivativePoint:
    return DerivativePoint(
        instrument_id="BTC-USDT-PERP",
        kind=DerivativeKind.FUNDING_RATE,
        timeframe=None,
        value=Decimal(str(value)),
        ts_event=ts,
        ts_published=ts,
        ts_available=ts,
        precision=PrecisionTier.SECOND_VERIFIED,
        ts_ingested=ts,
        ts_committed=ts,
    )


def _oi_point(ts: int, value: float) -> DerivativePoint:
    return DerivativePoint(
        instrument_id="BTC-USDT-PERP",
        kind=DerivativeKind.OPEN_INTEREST,
        timeframe="5m",
        value=Decimal(str(value)),
        ts_event=ts,
        ts_published=ts,
        ts_available=ts + 5 * MINUTE,
        precision=PrecisionTier.SECOND_VERIFIED,
        ts_ingested=ts + 5 * MINUTE,
        ts_committed=ts + 5 * MINUTE,
    )


class Freezer:
    def __init__(self, snapshot_id: str = "snap_scanner") -> None:
        self.snapshot_id = snapshot_id
        self.calls: list[tuple[str, str, int]] = []

    def __call__(self, instrument_id: str, timeframe: str, now: int) -> str:
        self.calls.append((instrument_id, timeframe, now))
        return self.snapshot_id


def _no_levels(**_: object) -> list[KeyLevel]:
    return []


def _engine(
    freezer: Freezer,
    *,
    key_levels: object | None = None,
    config: ScannerConfig | None = None,
) -> tuple[ScannerEngine, Freezer]:
    engine = ScannerEngine(
        now_ms=lambda: BASE_TS + 10 * 60 * MINUTE,
        config=config,
        freeze_snapshot=freezer,
        key_levels_provider=key_levels or _no_levels,  # type: ignore[arg-type]
    )
    return engine, freezer


def _seed(engine: ScannerEngine, bars: list[CompactBar]) -> None:
    engine.seed_bars("BTC-USDT-PERP", "1m", bars)


def test_liquidity_anomaly_emits_trigger_and_freezes_once() -> None:
    freezer = Freezer()
    engine, _ = _engine(freezer)
    bars = [_bar(BASE_TS + index * MINUTE) for index in range(40)]
    bars[-1] = _bar(BASE_TS + 39 * MINUTE, volume=100.0)
    _seed(engine, bars)

    triggers = engine.scan(["BTC-USDT-PERP"], now=BASE_TS + 40 * MINUTE)

    assert [trigger.kind for trigger in triggers] == [
        ScannerConditionKind.LIQUIDITY_ANOMALY
    ]
    trigger = triggers[0]
    assert trigger.route is ScannerTriggerRoute.LOCAL
    assert trigger.snapshot_id == "snap_scanner"
    assert trigger.trigger_id.startswith("scn_")
    assert freezer.calls == [("BTC-USDT-PERP", "1m", BASE_TS + 40 * MINUTE)]
    assert any(item.metric == "zscore" for item in trigger.evidence)
    assert "流动性" in trigger.tags


def test_volatility_expansion_uses_atr_and_percentile() -> None:
    freezer = Freezer()
    engine, _ = _engine(freezer)
    bars = [_bar(BASE_TS + index * MINUTE) for index in range(40)]
    bars[-1] = _bar(
        BASE_TS + 39 * MINUTE,
        close=105.0,
        high=110.0,
        low=90.0,
        volume=11.0,
    )
    _seed(engine, bars)

    triggers = engine.scan(["BTC-USDT-PERP"], now=BASE_TS + 40 * MINUTE)

    kinds = [trigger.kind for trigger in triggers]
    assert ScannerConditionKind.VOLATILITY_EXPANSION in kinds
    trigger = next(
        item
        for item in triggers
        if item.kind is ScannerConditionKind.VOLATILITY_EXPANSION
    )
    assert any(item.metric == "atr" for item in trigger.evidence)
    assert trigger.snapshot_id == "snap_scanner"


def test_funding_extreme_uses_settlement_history_and_skips_thin_history() -> None:
    freezer = Freezer()
    engine, _ = _engine(freezer)
    _seed(
        engine,
        [_bar(BASE_TS + index * MINUTE) for index in range(3)],
    )
    engine.seed_derivatives(
        "BTC-USDT-PERP",
        [_funding_point(BASE_TS + index * 8 * 3_600_000, 0.0001) for index in range(30)]
        + [_funding_point(BASE_TS + 30 * 8 * 3_600_000, 0.002)],
    )

    triggers = engine.scan(
        ["BTC-USDT-PERP"], now=BASE_TS + 30 * 8 * 3_600_000 + 1
    )

    funding = [
        trigger
        for trigger in triggers
        if trigger.kind is ScannerConditionKind.FUNDING_EXTREME
    ]
    assert len(funding) == 1
    assert any(item.metric == "funding_rate" for item in funding[0].evidence)

    thin = ScannerEngine(
        now_ms=lambda: BASE_TS,
        freeze_snapshot=freezer,
    )
    thin.seed_bars("BTC-USDT-PERP", "1m", [_bar(BASE_TS)])
    thin.seed_derivatives(
        "BTC-USDT-PERP",
        [_funding_point(BASE_TS - MINUTE, 0.01)],
    )
    assert thin.scan(["BTC-USDT-PERP"], now=BASE_TS) == []


def test_oi_divergence_requires_opposite_price_and_oi_direction() -> None:
    freezer = Freezer()
    engine, _ = _engine(freezer)
    bars = [_bar(BASE_TS + index * MINUTE, close=100.0 - index * 0.2) for index in range(5)]
    _seed(engine, bars)
    oi_ts = [BASE_TS + index * 5 * MINUTE for index in range(5)]
    engine.seed_derivatives(
        "BTC-USDT-PERP",
        [_oi_point(ts, 1_000.0 + index * 50.0) for index, ts in enumerate(oi_ts)],
    )

    now = oi_ts[-1] + 10 * MINUTE
    triggers = engine.scan(["BTC-USDT-PERP"], now=now)

    oi = [
        trigger
        for trigger in triggers
        if trigger.kind is ScannerConditionKind.OI_DIVERGENCE
    ]
    assert len(oi) == 1
    assert "价跌仓增" in oi[0].summary

    aligned = ScannerEngine(now_ms=lambda: now, freeze_snapshot=freezer)
    aligned.seed_bars("BTC-USDT-PERP", "1m", bars)
    aligned.seed_derivatives(
        "BTC-USDT-PERP",
        [_oi_point(ts, 1_000.0 - index * 50.0) for index, ts in enumerate(oi_ts)],
    )
    assert aligned.scan(["BTC-USDT-PERP"], now=now) == []


def test_oi_tick_bucket_supplements_derivative_series() -> None:
    freezer = Freezer()
    engine, _ = _engine(freezer)
    engine.seed_bars(
        "BTC-USDT-PERP",
        "1m",
        [_bar(BASE_TS + index * MINUTE, close=100.0 - index * 2.0) for index in range(5)],
    )
    oi_base = (BASE_TS // (5 * MINUTE) + 1) * 5 * MINUTE
    for index in range(4):
        engine.handle_event(
            DerivativeTickEvent(
                instrument_id="BTC-USDT-PERP",
                open_interest=Decimal(str(1_000 + index * 100)),
                ts_event=oi_base + index * 5 * MINUTE,
                ts_ingested=oi_base + index * 5 * MINUTE + 1_000,
            )
        )

    triggers = engine.scan(["BTC-USDT-PERP"], now=BASE_TS + 30 * MINUTE)

    assert ScannerConditionKind.OI_DIVERGENCE in {trigger.kind for trigger in triggers}


def _key_level(strength: float = 0.9, price: float = 100.0) -> KeyLevel:
    return KeyLevel(
        level_id="lvl_test",
        instrument_id="BTC-USDT-PERP",
        kind=KeyLevelKind.SWING_HIGH,
        category=KeyLevelCategory.STRUCTURE,
        evidence_class=EvidenceClass.OBSERVED,
        timeframe="1m",
        price=Decimal(str(price)),
        strength=strength,
        status=KeyLevelStatus.ACTIVE,
        ts_available=BASE_TS,
        formed_ts=BASE_TS,
        method_version="test",
        parameters={},
        touch_count=1,
        volume_weight=Decimal("1"),
    )


def test_key_level_touch_requires_strength_and_confirmed_bar() -> None:
    freezer = Freezer()

    def provider(**_: object) -> list[KeyLevel]:
        return [_key_level()]

    engine, _ = _engine(freezer, key_levels=provider)
    _seed(
        engine,
        [
            _bar(BASE_TS, close=99.0, high=99.5, low=98.5),
            _bar(BASE_TS + 2 * MINUTE, close=99.95, high=100.0, low=99.5),
            _bar(BASE_TS + 3 * MINUTE, close=100.4, high=100.6, low=99.9),
        ],
    )
    triggers = engine.scan(["BTC-USDT-PERP"], now=BASE_TS + 4 * MINUTE)
    key = [
        trigger
        for trigger in triggers
        if trigger.kind is ScannerConditionKind.KEY_LEVEL_EVENT
    ]
    assert len(key) == 1
    assert key[0].title == "关键位触及"

    weak = ScannerEngine(
        now_ms=lambda: BASE_TS + 4 * MINUTE,
        freeze_snapshot=freezer,
        key_levels_provider=lambda **_: [_key_level(0.4)],
    )
    weak.seed_bars(
        "BTC-USDT-PERP",
        "1m",
        [
            _bar(BASE_TS, close=99.0),
            _bar(BASE_TS + 2 * MINUTE, close=99.2),
            _bar(BASE_TS + 3 * MINUTE, close=100.4),
        ],
    )
    assert weak.scan(["BTC-USDT-PERP"], now=BASE_TS + 4 * MINUTE) == []


def test_same_bar_between_kinds_reuses_one_snapshot() -> None:
    freezer = Freezer()
    engine, _ = _engine(freezer)
    bars = [_bar(BASE_TS + index * MINUTE) for index in range(40)]
    bars[-1] = _bar(
        BASE_TS + 39 * MINUTE,
        close=108.0,
        high=110.0,
        low=95.0,
        volume=120.0,
    )
    _seed(engine, bars)

    triggers = engine.scan(["BTC-USDT-PERP"], now=BASE_TS + 40 * MINUTE)

    assert len(triggers) >= 2
    assert {trigger.snapshot_id for trigger in triggers} == {"snap_scanner"}
    assert len(freezer.calls) == 1


def test_stale_and_feed_gap_skip_without_false_positive() -> None:
    freezer = Freezer()
    engine, _ = _engine(freezer)
    bars = [_bar(BASE_TS + index * MINUTE) for index in range(40)]
    bars[-1] = _bar(BASE_TS + 39 * MINUTE, volume=100.0)
    _seed(engine, bars)
    engine.handle_event(
        FeedHealthEvent(
            instrument_id="BTC-USDT-PERP",
            timeframe="1m",
            status=FeedHealthStatus.STALE,
            ts=BASE_TS + 40 * MINUTE,
            stale_since=BASE_TS + 39 * MINUTE,
        )
    )

    assert (
        engine.scan(["BTC-USDT-PERP"], now=BASE_TS + 41 * MINUTE) == []
    )
    assert freezer.calls == []

    gap_engine, _ = _engine(freezer)
    gap_engine.seed_bars("BTC-USDT-PERP", "1m", bars)
    gap_engine.handle_event(
        _kline(_bar(BASE_TS + 40 * MINUTE, status=BarStatus.FEED_GAP))
    )
    assert (
        gap_engine.scan(["BTC-USDT-PERP"], now=BASE_TS + 42 * MINUTE) == []
    )


def test_cooldown_is_per_instrument_and_per_kind() -> None:
    freezer = Freezer()
    engine, _ = _engine(freezer)
    bars = [_bar(BASE_TS + index * MINUTE) for index in range(50)]
    bars[39] = _bar(BASE_TS + 39 * MINUTE, volume=100.0)
    _seed(engine, bars)

    first = engine.scan(["BTC-USDT-PERP"], now=BASE_TS + 40 * MINUTE)
    assert first

    # 新柱、仍在冷却内：不重复。
    engine.handle_event(_kline(_bar(BASE_TS + 41 * MINUTE, volume=120.0)))
    assert engine.scan(["BTC-USDT-PERP"], now=BASE_TS + 43 * MINUTE) == []

    # 冷却结束后的新柱：允许再次命中。
    engine.handle_event(_kline(_bar(BASE_TS + 55 * MINUTE, volume=130.0)))
    later = engine.scan(["BTC-USDT-PERP"], now=BASE_TS + 5 * MINUTE + 56 * MINUTE)
    assert any(
        trigger.kind is ScannerConditionKind.LIQUIDITY_ANOMALY
        for trigger in later
    )


def test_scan_is_deterministic_and_ordered() -> None:
    bars = [_bar(BASE_TS + index * MINUTE) for index in range(40)]
    bars[-1] = _bar(BASE_TS + 39 * MINUTE, volume=200.0)
    now = BASE_TS + 40 * MINUTE
    freezer = Freezer()
    engine, _ = _engine(freezer)
    _seed(engine, bars)
    engine.seed_bars("ETH-USDT-PERP", "1m", bars)

    first = engine.scan(["ETH-USDT-PERP", "BTC-USDT-PERP"], now=now)
    assert [trigger.instrument_id for trigger in first] == [
        "BTC-USDT-PERP",
        "ETH-USDT-PERP",
    ]

    other, _ = _engine(Freezer())
    other.seed_bars("BTC-USDT-PERP", "1m", bars)
    other.seed_bars("ETH-USDT-PERP", "1m", bars)
    second = other.scan(["BTC-USDT-PERP", "ETH-USDT-PERP"], now=now)
    assert [item.model_dump() for item in first] == [
        item.model_dump() for item in second
    ]


def test_manual_scan_evaluates_a_composite_rule_only_on_visible_confirmed_bars() -> None:
    # alerts-and-scanning/01: 扫描器复用规则，在确认时点冻结证据。
    from talaria.foundation.rules import Rule

    engine, freezer = _engine(Freezer())
    instrument = "BTC-USDT-PERP"
    rule = Rule.model_validate({"version": "1", "timing": "bar_close", "expression": {
        "op": "all", "args": [
            {"op": "compare", "test": "gt", "left": {"source": "bar", "field": "close"},
             "right": {"source": "constant", "value": "105"}},
            {"op": "compare", "test": "lt",
             "left": {"source": "derivative", "kind": "funding_rate"},
             "right": {"source": "constant", "value": "0"}},
        ],
    }})
    current = _bar(BASE_TS + MINUTE, close=110)
    _seed(engine, [_bar(BASE_TS)])
    engine.seed_derivatives(instrument, [_funding_point(BASE_TS, -0.0001)])
    engine.handle_event(_kline(current).model_copy(update={"confirmed": False}))
    assert engine.scan([instrument], now=current.ts_available, rule=rule) == []
    assert engine.scan([instrument], now=current.ts_available + engine.config.focus_ttl_ms, rule=rule) == []

    engine.handle_event(_kline(current))
    assert engine.scan([instrument], now=current.ts_available - 1, rule=rule) == []
    hits = engine.scan([instrument], now=current.ts_available, rule=rule)
    assert [hit.kind for hit in hits] == [ScannerConditionKind.RULE]
    assert hits[0].snapshot_id == "snap_scanner"
    assert freezer.calls == [(instrument, "1m", current.ts_available)]
    assert engine.scan([instrument], now=current.ts_available, rule=rule) == []


def _rule(expression):
    from talaria.foundation.rules import Rule

    return Rule.model_validate(dict(version="1", timing="bar_close", expression=expression))


def _scan(engine, rule, now):
    return engine.scan(["BTC-USDT-PERP"], now=now, rule=rule)


def test_late_scan_bar_uses_levels_at_close():
    current = _bar(BASE_TS).model_copy(update={"ts_available": BASE_TS + 3 * MINUTE})
    def levels(instrument, timeframe, cutoff):
        return [_key_level()] if cutoff == BASE_TS + MINUTE else []
    engine = ScannerEngine(now_ms=lambda: current.ts_available, freeze_snapshot=Freezer(), rule_levels_at=levels)
    _seed(engine, [current])
    rule = _rule(dict(op="level_event", event="TOUCH", level_id="lvl_test"))
    assert len(_scan(engine, rule, current.ts_available)) == 1


def test_scanner_replay_consumes_the_first_completed_retest():
    engine, _ = _engine(Freezer(), key_levels=lambda **_: [
        _key_level().model_copy(update={"status": KeyLevelStatus.BROKEN})
    ], config=ScannerConfig(cooldown_ms=1))
    rule = _rule(dict(op="level_event", level_id="lvl_test", event="RETEST"))
    _seed(engine, [_bar(BASE_TS, close=101, low=101)])
    counts = []
    for offset in [1, 2, 3]:
        bar = _bar(BASE_TS + offset * MINUTE, close=101, low=99)
        engine.handle_event(_kline(bar))
        counts.append(len(_scan(engine, rule, bar.ts_available)))
    assert counts == [1, 0, 0]


@pytest.mark.parametrize("scenario", ["gap", "period", "revision"])
def test_scan_indicators_use_visible_history_including_warmup(scenario):
    engine, _ = _engine(Freezer())
    period = 650 if scenario == "period" else 2
    rule = _rule(dict(op="compare", test="eq", left=dict(source="indicator", name="SMA", params={"period": period}),
                      right=dict(source="constant", value="100")))
    bars = [_bar(BASE_TS + i * MINUTE) for i in range(period)]
    if scenario == "gap":
        bars.insert(1, _bar(BASE_TS + MINUTE, status=BarStatus.FEED_GAP))
        bars[-1] = _bar(BASE_TS + 2 * MINUTE)
    _seed(engine, bars)
    if scenario == "revision":
        revised = _bar(BASE_TS, close=500).model_copy(update={"ts_available": BASE_TS + 3 * MINUTE})
        engine.handle_event(_kline(revised).model_copy(update={"revision": 1}))
    assert bool(_scan(engine, rule, bars[-1].ts_available + MINUTE)) == (scenario != "gap")


@pytest.mark.parametrize("kind", ["mark_price", "open_interest"])
def test_price_and_oi_rules_read_visible_ticker(kind):
    engine, _ = _engine(Freezer())
    _seed(engine, [_bar(BASE_TS)])
    for delay, value in [(0, "110"), (2 * MINUTE, "80")]:
        engine.handle_event(DerivativeTickEvent(instrument_id="BTC-USDT-PERP", **{kind: value},
                                                ts_event=BASE_TS + delay, ts_ingested=BASE_TS + delay))
    rule = _rule(dict(op="compare", test="gt", left=dict(source="derivative", kind=kind),
                      right=dict(source="constant", value="100")))
    assert len(_scan(engine, rule, BASE_TS + 3 * MINUTE)) == 1


def test_manual_scan_freezes_the_as_of_used_to_evaluate_the_rule():
    engine, freezer = _engine(Freezer())
    bar = _bar(BASE_TS)
    _seed(engine, [bar])
    rule = _rule(dict(op="compare", test="eq", left=dict(source="bar", field="close"),
                      right=dict(source="constant", value="100")))
    hit, = _scan(engine, rule, bar.ts_available + 4 * MINUTE)
    assert hit.snapshot_id == freezer.snapshot_id
    assert freezer.calls == [("BTC-USDT-PERP", "1m", bar.ts_available)]


def test_mark_quotes_do_not_evict_oi_gate_history():
    engine, _ = _engine(Freezer(), config=ScannerConfig(max_bars=5))
    _seed(engine, [_bar(BASE_TS + i * MINUTE, close=100 - i*2) for i in range(5)])
    start = (BASE_TS // (5 * MINUTE) + 1) * 5 * MINUTE
    for i in range(4):
        ts = start + i * 5 * MINUTE
        engine.handle_event(DerivativeTickEvent(instrument_id="BTC-USDT-PERP", open_interest=str(1000 + i*100),
                                                ts_event=ts, ts_ingested=ts))
        for offset in range(1, 26):
            engine.handle_event(DerivativeTickEvent(instrument_id="BTC-USDT-PERP", mark_price="95",
                                                    ts_event=ts+offset, ts_ingested=ts+offset))
    hits = engine.scan(["BTC-USDT-PERP"], now=BASE_TS + 30 * MINUTE)
    assert ScannerConditionKind.OI_DIVERGENCE in [hit.kind for hit in hits]


def test_retest_replay_observes_a_breakout_revision_before_the_pullback():
    engine, _ = _engine(Freezer(), key_levels=lambda **_: [
        _key_level().model_copy(update={"status": KeyLevelStatus.BROKEN})
    ])
    _seed(engine, [_bar(BASE_TS, close=100)])
    revised = _bar(BASE_TS, close=102, low=101).model_copy(update={"ts_available": BASE_TS + MINUTE + 1})
    engine.handle_event(_kline(revised).model_copy(update={"revision": 1}))
    _seed(engine, [_bar(BASE_TS + MINUTE, close=102, low=99)])
    rule = _rule(dict(op="level_event", level_id="lvl_test", event="RETEST"))
    assert len(_scan(engine, rule, BASE_TS + 2 * MINUTE)) == 1
