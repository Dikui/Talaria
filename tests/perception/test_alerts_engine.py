"""预警引擎：三类条件、状态机、STALE 恢复与实时/回放一致性。

来源：任务单 13、docs/architecture.md §3.2.3 与 spec 的盘面预警需求。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from talaria.foundation.alerts import (
    AlertDirection,
    AlertMetric,
    AlertOverlayMode,
    AlertRecord,
    AlertStatus,
)
from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.drawings import (
    DrawingKind,
    DrawingPoint,
    DrawingRecord,
    DrawingStyle,
)
from talaria.foundation.feed import (
    DerivativeTickEvent,
    FeedHealthEvent,
    FeedHealthStatus,
    KlineEvent,
    OrderBookEvent,
)
from talaria.foundation.types import PrecisionTier
from talaria.foundation.rules import Rule
from talaria.perception.alerts import AlertEngine
from talaria.replay import ReplayFeed

pytestmark = pytest.mark.small

INSTRUMENT = "BTC-USDT-PERP"
TIMEFRAME = "1m"
BASE_TS = 1_763_596_800_000
MINUTE = 60_000


class Clock:
    def __init__(self, now: int = 1_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def _bar(
    ts: int,
    close: str,
    *,
    high: str | None = None,
    low: str | None = None,
) -> CompactBar:
    return CompactBar(
        timestamp=ts,
        open=close,
        high=high or close,
        low=low or close,
        close=close,
        volume="1",
        turnover="1",
        status=BarStatus.NORMAL,
        ts_available=ts + MINUTE,
        precision=PrecisionTier.SECOND_VERIFIED,
    )


def _kline(
    ts: int,
    close: str,
    *,
    high: str | None = None,
    low: str | None = None,
    confirmed: bool = True,
    revision: int = 0,
    timeframe: str = TIMEFRAME,
) -> KlineEvent:
    return KlineEvent(
        instrument_id=INSTRUMENT,
        timeframe=timeframe,
        bar=_bar(ts, close, high=high, low=low),
        confirmed=confirmed,
        revision=revision,
    )


def _book(bid: str, ask: str) -> OrderBookEvent:
    return OrderBookEvent(
        instrument_id=INSTRUMENT,
        bid_price=bid,
        bid_size="1",
        ask_price=ask,
        ask_size="1",
        ts_event=BASE_TS,
        ts_ingested=BASE_TS + 1,
    )


def _health(status: FeedHealthStatus, last_event_ts: int | None = None) -> FeedHealthEvent:
    return FeedHealthEvent(
        instrument_id=INSTRUMENT,
        timeframe=TIMEFRAME,
        status=status,
        ts=BASE_TS,
        last_event_ts=last_event_ts,
        stale_since=BASE_TS if status is FeedHealthStatus.STALE else None,
        reconnect_count=0,
    )


def _metric_alert(
    *,
    alert_id: str = "alr_test",
    direction: AlertDirection = AlertDirection.ABOVE,
    metric: AlertMetric = AlertMetric.LAST_PRICE,
    threshold: str = "100",
    status: AlertStatus = AlertStatus.ARMED,
    repeat: bool = True,
    cooldown_ms: int = 60_000,
    expires_at: int | None = None,
) -> AlertRecord:
    return AlertRecord(
        alert_id=alert_id,
        instrument_id=INSTRUMENT,
        timeframe=TIMEFRAME,
        condition=Rule.model_validate({
            "version": "1", "timing": "event",
            "expression": {
                "op": "compare", "test": ("gte" if direction == "ABOVE" else "lte"),
                "left": {"source": "event", "metric": metric},
                "right": {"source": "constant", "value": threshold},
            },
        }),
        status=status,
        repeat=repeat,
        cooldown_ms=cooldown_ms,
        expires_at=expires_at,
        created_ts=0,
        updated_ts=0,
    )


def _drawing(
    *,
    drawing_id: str = "drw_alert",
    kind: DrawingKind = DrawingKind.HORIZONTAL_LINE,
    points: list[tuple[int, str]] | None = None,
    deleted_at: int | None = None,
) -> DrawingRecord:
    anchors = points or [(BASE_TS, "100")]
    return DrawingRecord(
        drawing_id=drawing_id,
        instrument_id=INSTRUMENT,
        kind=kind,
        visible_timeframes=[TIMEFRAME],
        points=[DrawingPoint(ts=ts, price=price) for ts, price in anchors],
        text="",
        style=DrawingStyle(),
        version=1,
        created_ts=0,
        updated_ts=0,
        deleted_at=deleted_at,
    )


def _overlay_alert(
    *,
    mode: AlertOverlayMode = AlertOverlayMode.CLOSE_BREAK,
    direction: AlertDirection = AlertDirection.ABOVE,
    repeat: bool = True,
    cooldown_ms: int = 60_000,
) -> AlertRecord:
    return AlertRecord(
        alert_id="alr_overlay",
        instrument_id=INSTRUMENT,
        timeframe=TIMEFRAME,
        condition=Rule.model_validate({
            "version": "1", "timing": ("event" if mode == "TOUCH" else "bar_close"),
            "expression": {"op": "overlay", "direction": direction, "drawing_id": 'drw_alert', "mode": mode},
        }),
        status=AlertStatus.ARMED,
        repeat=repeat,
        cooldown_ms=cooldown_ms,
        created_ts=0,
        updated_ts=0,
    )


class Harness:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.triggers: list[tuple[str, str]] = []
        self.engine = AlertEngine(
            now_ms=clock,
            freeze_snapshot=lambda alert, now: f"snap-{alert.alert_id}-{now}",
            on_trigger=self._capture,
        )

    def _capture(self, trigger, alert: AlertRecord) -> None:
        self.triggers.append((trigger.trigger_id, trigger.message))
        assert trigger.snapshot_id.startswith("snap-")

    @property
    def messages(self) -> Iterator[str]:
        return (message for _, message in self.triggers)


def test_metric_threshold_triggers_on_cross_and_freezes_snapshot() -> None:
    clock = Clock(5_000)
    harness = Harness(clock)
    alert = _metric_alert()
    harness.engine.put(alert, reset=True)

    harness.engine.handle_event(_kline(BASE_TS, "99"))
    assert harness.triggers == []

    clock.now = 6_000
    harness.engine.handle_event(_kline(BASE_TS + MINUTE, "101"))

    assert len(harness.triggers) == 1
    assert harness.triggers[0][1] == "价格上穿 100"
    assert alert.status is AlertStatus.COOLDOWN
    assert alert.next_arm_ts == BASE_TS + 3 * MINUTE
    assert alert.trigger_count == 1
    assert alert.last_triggered_ts == BASE_TS + 2 * MINUTE


def test_metric_below_uses_order_book_bid_price() -> None:
    clock = Clock()
    harness = Harness(clock)
    alert = _metric_alert(
        direction=AlertDirection.BELOW, metric=AlertMetric.BID_PRICE, threshold="100"
    )
    harness.engine.put(alert, reset=True)

    harness.engine.handle_event(_book("101", "101.1"))
    harness.engine.handle_event(_book("99", "99.1"))

    assert list(harness.messages) == ["买一价下穿 100"]


def test_indicator_signal_uses_python_operator_series() -> None:
    clock = Clock()
    harness = Harness(clock)
    alert = AlertRecord(
        alert_id="alr_rsi",
        instrument_id=INSTRUMENT,
        timeframe=TIMEFRAME,
        condition=Rule.model_validate({
            "version": "1", "timing": "bar_close",
            "expression": {
                "op": "compare", "test": "gte",
                "left": {"source": "indicator", "name": "SMA", "params": {"period": 2}},
                "right": {"source": "constant", "value": '12'},
            },
        }),
        status=AlertStatus.ARMED,
        repeat=True,
        cooldown_ms=0,
        created_ts=0,
        updated_ts=0,
    )
    harness.engine.put(alert, reset=True)

    harness.engine.handle_event(_kline(BASE_TS, "10"))
    harness.engine.handle_event(_kline(BASE_TS + MINUTE, "10"))
    assert harness.triggers == []

    harness.engine.handle_event(_kline(BASE_TS + 2 * MINUTE, "20"))

    assert list(harness.messages) == ["SMA(2) 上穿 12"]


def test_overlay_touch_fires_without_close_beyond_line() -> None:
    clock = Clock()
    harness = Harness(clock)
    harness.engine.refresh_drawings([_drawing()])
    alert = _overlay_alert(mode=AlertOverlayMode.TOUCH)
    harness.engine.put(alert, reset=True)

    harness.engine.handle_event(_kline(BASE_TS, "98", high="99"))
    harness.engine.handle_event(_kline(BASE_TS + MINUTE, "99", high="100.5"))

    assert list(harness.messages) == ["价格触碰水平线 100"]


def test_overlay_close_break_uses_only_confirmed_closes() -> None:
    clock = Clock()
    harness = Harness(clock)
    harness.engine.refresh_drawings([_drawing()])
    alert = _overlay_alert()
    harness.engine.put(alert, reset=True)

    harness.engine.handle_event(_kline(BASE_TS, "99", confirmed=False))
    harness.engine.handle_event(_kline(BASE_TS, "101", confirmed=False))
    harness.engine.handle_event(_kline(BASE_TS + MINUTE, "99", confirmed=True))
    assert harness.triggers == []

    harness.engine.handle_event(_kline(BASE_TS + 2 * MINUTE, "101", confirmed=True))

    assert list(harness.messages) == ["价格收盘突破水平线 100"]


def test_overlay_uses_latest_geometry_after_drawing_edit() -> None:
    clock = Clock()
    harness = Harness(clock)
    harness.engine.refresh_drawings([_drawing(points=[(BASE_TS, "100")])])
    alert = _overlay_alert(cooldown_ms=0)
    harness.engine.put(alert, reset=True)

    harness.engine.handle_event(_kline(BASE_TS, "99"))
    harness.engine.handle_event(_kline(BASE_TS + MINUTE, "101"))
    assert len(harness.triggers) == 1

    harness.engine.refresh_drawings([_drawing(points=[(BASE_TS, "105")])])
    harness.engine.handle_event(_kline(BASE_TS + 2 * MINUTE, "104"))
    assert len(harness.triggers) == 1

    harness.engine.handle_event(_kline(BASE_TS + 3 * MINUTE, "106"))
    assert len(harness.triggers) == 2
    assert harness.triggers[-1][1] == "价格收盘突破水平线 105"


def test_single_shot_alert_expires_after_first_trigger() -> None:
    clock = Clock()
    harness = Harness(clock)
    alert = _metric_alert(repeat=False)
    harness.engine.put(alert, reset=True)

    harness.engine.handle_event(_kline(BASE_TS, "99"))
    harness.engine.handle_event(_kline(BASE_TS + MINUTE, "101"))
    assert alert.status is AlertStatus.EXPIRED

    clock.now = 10_000_000
    assert harness.engine.tick() == []
    harness.engine.handle_event(_kline(BASE_TS + 2 * MINUTE, "99"))
    harness.engine.handle_event(_kline(BASE_TS + 3 * MINUTE, "101"))

    assert alert.status is AlertStatus.EXPIRED
    assert len(harness.triggers) == 1


def test_cooldown_rearms_and_requires_a_fresh_false_to_true_edge() -> None:
    clock = Clock(1_000)
    harness = Harness(clock)
    alert = _metric_alert(cooldown_ms=1_000)
    harness.engine.put(alert, reset=True)

    harness.engine.handle_event(_kline(BASE_TS, "99"))
    clock.now = 2_000
    harness.engine.handle_event(_kline(BASE_TS + MINUTE, "101"))
    assert alert.status is AlertStatus.COOLDOWN
    visible = BASE_TS + 2 * MINUTE
    assert alert.next_arm_ts == visible + 1_000

    assert harness.engine.tick(visible + 500) == []
    assert alert.status is AlertStatus.COOLDOWN
    assert harness.engine.tick(visible + 1_000) == [alert]
    assert alert.status is AlertStatus.ARMED

    harness.engine.handle_event(_kline(BASE_TS + 2 * MINUTE, "102"))
    assert len(harness.triggers) == 1

    harness.engine.handle_event(_kline(BASE_TS + 3 * MINUTE, "99"))
    harness.engine.handle_event(_kline(BASE_TS + 4 * MINUTE, "101"))
    assert len(harness.triggers) == 2


def test_stale_suspends_and_recovery_rebaselines_without_false_trigger() -> None:
    clock = Clock()
    harness = Harness(clock)
    alert = _metric_alert()
    harness.engine.put(alert, reset=True)

    harness.engine.handle_event(_kline(BASE_TS, "99"))
    harness.engine.handle_event(_health(FeedHealthStatus.STALE))

    outage_bar = BASE_TS + MINUTE
    harness.engine.handle_event(_kline(outage_bar, "101"))
    assert harness.triggers == []

    harness.engine.handle_event(_health(FeedHealthStatus.LIVE, last_event_ts=outage_bar))

    harness.engine.handle_event(_kline(BASE_TS + 2 * MINUTE, "102"))
    assert harness.triggers == []

    harness.engine.handle_event(_kline(BASE_TS + 3 * MINUTE, "95"))
    harness.engine.handle_event(_kline(BASE_TS + 4 * MINUTE, "103"))

    assert len(harness.triggers) == 1
    assert alert.trigger_count == 1


def test_draft_never_triggers_even_after_cross() -> None:
    clock = Clock()
    harness = Harness(clock)
    alert = _metric_alert(status=AlertStatus.DRAFT)
    harness.engine.put(alert, reset=True)

    harness.engine.handle_event(_kline(BASE_TS, "99"))
    harness.engine.handle_event(_kline(BASE_TS + MINUTE, "101"))

    assert harness.triggers == []
    assert alert.status is AlertStatus.DRAFT


def test_expires_at_moves_armed_alert_to_expired() -> None:
    clock = Clock(500)
    harness = Harness(clock)
    alert = _metric_alert(expires_at=1_000)
    harness.engine.put(alert, reset=True)

    assert harness.engine.tick(999) == []
    assert harness.engine.tick(1_000) == [alert]
    assert alert.status is AlertStatus.EXPIRED


def test_replay_and_live_produce_the_same_triggers(tmp_path: Path) -> None:
    bars = [(0, "99"), (1, "101"), (2, "99")]
    lines = [
        {
            "offset_ms": offset * 1_000,
            "type": "kline",
            "event": {
                "instrument_id": INSTRUMENT,
                "timeframe": TIMEFRAME,
                "bar": _bar(BASE_TS + offset * MINUTE, close).model_dump(mode="json"),
                "confirmed": True,
                "revision": 0,
            },
        }
        for offset, close in bars
    ]
    path = tmp_path / "alerts.jsonl"
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )

    async def collect() -> list[KlineEvent]:
        feed = ReplayFeed(path, speed=1_000.0, sleep=_no_sleep)
        await feed.connect()
        await feed.subscribe(instrument_id=INSTRUMENT, timeframe=TIMEFRAME)
        events = [await feed.recv(), await feed.recv(), await feed.recv()]
        await feed.close()
        assert all(isinstance(event, KlineEvent) for event in events)
        return events  # type: ignore[return-value]

    replay_engine = AlertEngine(now_ms=lambda: 9_000)
    replay_alert = _metric_alert(cooldown_ms=0)
    replay_engine.put(replay_alert, reset=True)
    for event in asyncio.run(collect()):
        replay_engine.handle_event(event)

    live_engine = AlertEngine(now_ms=lambda: 9_000)
    live_alert = _metric_alert(cooldown_ms=0)
    live_engine.put(live_alert, reset=True)
    for offset, close in bars:
        live_engine.handle_event(_kline(BASE_TS + offset * MINUTE, close))

    assert replay_alert.trigger_count == live_alert.trigger_count == 1
    assert replay_alert.status is live_alert.status is AlertStatus.ARMED


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.mark.parametrize("seed_from_history", [False, True])
@pytest.mark.parametrize("final_event", ["close", "revision", "gap"])
def test_session_vwap_alert_keeps_utc_anchor_beyond_600_bars(
    seed_from_history, final_event
) -> None:
    from talaria.foundation.operators import compute_series, spec_from_params

    clock = Clock()
    midnight = BASE_TS // 86_400_000 * 86_400_000
    history = [_kline(midnight + index * MINUTE, "10") for index in range(-1, 601)]
    if final_event == "gap":
        gap = history[-2].bar.model_copy(
            update=dict(
                status=BarStatus.FEED_GAP, open=None, high=None, low=None,
                close=None, volume=None, turnover=None,
            )
        )
        history[-2] = history[-2].model_copy(update={"bar": gap})
    triggers = []
    engine = AlertEngine(
        now_ms=clock,
        history_provider=lambda *_: [event.bar for event in history] if seed_from_history else [],
        on_trigger=lambda trigger, _: triggers.append(trigger),
    )
    alert = _metric_alert(threshold="12", cooldown_ms=0)
    alert.condition = Rule.model_validate({
        "version": "1", "timing": "bar_close",
        "expression": {
            "op": "compare", "test": "gte",
            "left": {"source": "indicator", "name": "VWAP", "params": {"period": 1, "anchor": "session"}},
            "right": {"source": "constant", "value": '12'},
        },
    })
    engine.put(alert)
    for event in history[-1:] if seed_from_history else history:
        clock.now = event.bar.ts_available
        engine.handle_event(event)
    clock.now += MINUTE
    if final_event == "revision":
        event = _kline(midnight, "10000", revision=1)
        history[1] = event
    else:
        event = _kline(midnight + 601 * MINUTE, "10000")
        history.append(event)
    engine.handle_event(event)

    expected = compute_series(
        [event.bar for event in history], spec_from_params("VWAP")
    ).points[-1]["value"]
    if final_event == "gap":
        assert expected is None
        assert triggers == []
    else:
        assert [(trigger.triggered_ts, trigger.observed) for trigger in triggers] == [
            (clock.now, expected)
        ]



def test_composite_rule_can_trigger_on_first_confirmed_cross_after_warmup() -> None:
    # alerts-and-scanning/01: 上穿有两根已确认柱作为判断依据。
    clock = Clock(BASE_TS + 2 * MINUTE)
    trigger_times = []
    seed = [_bar(BASE_TS, "100")]
    engine = AlertEngine(
        now_ms=clock, history_provider=lambda *_: seed,
        on_trigger=lambda trigger, _: trigger_times.append(trigger.triggered_ts),
    )
    alert = _metric_alert()
    alert.condition = Rule.model_validate({
        "version": "1", "timing": "bar_close", "expression": {
            "op": "all", "args": [
                {"op": "compare", "test": "cross_above", "left": {"source": "bar", "field": "close"},
                 "right": {"source": "indicator", "name": "EMA", "params": {"period": 2}}},
                {"op": "compare", "test": "lt", "left": {"source": "bar", "field": "close"},
                 "right": {"source": "constant", "value": "115"}},
                {"op": "compare", "test": "lt",
                 "left": {"source": "derivative", "kind": "funding_rate"},
                 "right": {"source": "constant", "value": "0"}},
            ],
        },
    })
    engine.put(alert)
    for ts, rate in ((BASE_TS + MINUTE, "-0.001"), (clock.now + 1, "0.001")):
        from talaria.foundation.derivatives import DerivativePoint

        engine.seed_derivatives([DerivativePoint(
            instrument_id=INSTRUMENT, kind="funding_rate", settled=True, value=rate, ts_event=ts,
            ts_ingested=ts, ts_committed=ts, ts_available=ts,
        )])
    engine.handle_event(_kline(BASE_TS + MINUTE, "111", confirmed=False))
    assert trigger_times == []
    engine.handle_event(_kline(BASE_TS + MINUTE, "110", revision=1))
    assert trigger_times == [BASE_TS + 2 * MINUTE]


def _rule_alert(expression, timing="bar_close", cooldown_ms=0):
    alert = _metric_alert(cooldown_ms=cooldown_ms)
    alert.condition = Rule.model_validate(dict(version="1", timing=timing, expression=expression))
    return alert


def _compare(left, value, test="gt"):
    return dict(op="compare", test=test, left=left, right=dict(source="constant", value=value))


def test_late_first_confirmation_preserves_parent_indicator_trigger_trace():
    # 21b9b69^ 的 SMA(1) 读取窗口最后一根。迟到的中间槽从未成为最新观察。
    clock = Clock(BASE_TS + 4 * MINUTE)
    from talaria.workbench_api.state_repositories.alert_migration import migrate_rule

    records = []
    engine = AlertEngine(now_ms=clock, on_trigger=lambda trigger, _: records.append(trigger))
    alert = _metric_alert(cooldown_ms=0)
    alert.condition = migrate_rule(dict(kind="INDICATOR_SIGNAL", indicator="SMA", indicator_period=1,
                                        direction="ABOVE", threshold="100"), predates_pine=False)
    engine.put(alert)
    counts = []
    for offset, close in [(0, "90"), (2, "95"), (1, "110"), (3, "120")]:
        engine.handle_event(_kline(BASE_TS + offset * MINUTE, close))
        counts.append(alert.trigger_count)
    assert counts == [0, 0, 0, 1]
    assert [(t.triggered_ts, str(t.observed), t.message) for t in records] == [
        (1763597040000, "120", "SMA(1) 上穿 100"),
    ]


def test_old_revision_never_combines_revised_geometry_with_latest_close():
    harness = Harness(Clock(BASE_TS + 4 * MINUTE))
    harness.engine.refresh_drawings([_drawing()])
    alert = _rule_alert(dict(op="all", args=[
        dict(op="overlay", mode="CLOSE_BREAK", direction="ABOVE", drawing_id="drw_alert"),
        _compare(dict(source="bar", field="close"), "100", "lt"),
    ]))
    harness.engine.put(alert)
    for event in [_kline(BASE_TS, "95"), _kline(BASE_TS + MINUTE, "95"), _kline(BASE_TS, "110", revision=1)]:
        harness.engine.handle_event(event)
    assert alert.trigger_count == 0


@pytest.mark.parametrize("rearm", ["live", "cooldown", "already_true"])
def test_explicit_cross_requires_root_edge_after_rearm(rearm):
    clock = Clock(BASE_TS)
    harness = Harness(clock)
    cross = _compare(dict(source="bar", field="close"), "100", "cross_above")
    rule = dict(op="any", args=[cross, _compare(dict(source="bar", field="close"), "80")]) if rearm == "already_true" else cross
    alert = _rule_alert(rule, cooldown_ms=MINUTE if rearm == "cooldown" else 0)
    harness.engine.put(alert)
    def send(slot, close):
        clock.now = BASE_TS + (slot + 1) * MINUTE
        harness.engine.handle_event(_kline(BASE_TS + slot * MINUTE, close))

    send(0, "70")
    send(1, "75")
    if rearm == "already_true":
        send(2, "90")
        send(3, "110")
        assert alert.trigger_count == 1
    elif rearm == "live":
        harness.engine.handle_event(_health(FeedHealthStatus.STALE))
        harness.engine.handle_event(_health(FeedHealthStatus.LIVE))
        send(2, "110")
        assert alert.trigger_count == 0
    else:
        send(2, "110")
        # 冷却期间收到对最后一槽的修订，恢复后首个有效观察恰好上穿。
        harness.engine.handle_event(_kline(BASE_TS + 2 * MINUTE, "90", revision=1))
        send(3, "110")
        assert alert.trigger_count == 1


@pytest.mark.parametrize("negate", [False, True])
def test_partial_event_group_observes_false_to_true_on_the_same_input(negate):
    # any(bid>100, rate>0) 与 not(all(not bid>100, not rate>0)) 等价。
    args = [_compare(dict(source="event", metric=metric), threshold)
            for metric, threshold in [("bid_price", "100"), ("funding_rate", "0")]]
    group = dict(op="any", args=args)
    if negate:
        group = dict(op="not", args=[dict(op="all", args=[dict(op="not", args=[arg]) for arg in args])])
    alert = _rule_alert(group, timing="event")
    harness = Harness(Clock(BASE_TS))
    harness.engine.put(alert)
    for bid in ["99", "101", "102", "99", "101"]:
        harness.engine.handle_event(_book(bid, bid))
    assert alert.trigger_count == 2


def test_early_confirmation_freezes_and_starts_cooldown_at_visibility():
    from talaria.foundation.feed import ReceivedKlineEvent

    clock = Clock(BASE_TS + 2 * MINUTE - 250)
    frozen = []
    engine = AlertEngine(now_ms=clock, freeze_snapshot=lambda _, ts: frozen.append(ts) or "snap")
    alert = _metric_alert()
    engine.put(alert)
    engine.handle_event(_kline(BASE_TS, "90"))
    event = _kline(BASE_TS + MINUTE, "110")
    engine.handle_event(ReceivedKlineEvent(**event.model_dump(), ts_ingested=clock.now))
    assert frozen == [BASE_TS + 2 * MINUTE]
    assert alert.next_arm_ts == frozen[0] + alert.cooldown_ms


def test_predicted_funding_updates_do_not_create_settlement_observations():
    alert = _rule_alert(_compare(dict(source="derivative", kind="funding_rate"), "0"))
    engine = AlertEngine(now_ms=Clock(BASE_TS + 2 * MINUTE))
    engine.put(alert)
    for index, rate in enumerate(["-0.001", "0.001"]):
        ts = BASE_TS + index * MINUTE
        engine.handle_event(DerivativeTickEvent(instrument_id=INSTRUMENT, funding_rate=rate,
                                                ts_event=ts, ts_ingested=ts))
        engine.handle_event(_kline(ts, "100"))
    assert alert.trigger_count == 0


@pytest.mark.regression
def test_snapshot_backfill_is_observed_on_the_next_feed_event():
    # alerts-and-scanning/01 × data-access/08：冻结第一条预警的证据会发布一笔历史结算。
    from talaria.foundation.derivatives import DerivativePoint

    def settlement(ts, rate):
        return DerivativePoint(instrument_id=INSTRUMENT, kind="funding_rate", value=rate, settled=True,
                               ts_event=ts, ts_available=ts, ts_ingested=ts, ts_committed=ts)

    archive = [settlement(BASE_TS, "-0.001")]
    price = _rule_alert(_compare(dict(source="bar", field="close"), "100"))
    funding = _rule_alert(_compare(dict(source="derivative", kind="funding_rate"), "0"))
    price.alert_id, funding.alert_id = "a_price", "b_funding"

    def freeze(alert, cutoff):
        if alert.alert_id == price.alert_id:
            archive.append(settlement(BASE_TS + MINUTE, "0.001"))
        return "snap"

    engine = AlertEngine(now_ms=Clock(BASE_TS), read_archived_derivatives=lambda *_: archive,
                         freeze_snapshot=freeze)
    engine.load([price, funding])
    engine.handle_event(_kline(BASE_TS, "90"))
    engine.handle_event(_kline(BASE_TS + MINUTE, "105"))
    assert (price.trigger_count, funding.trigger_count) == (1, 0)
    engine.handle_event(_kline(BASE_TS + 2 * MINUTE, "105"))
    assert funding.trigger_count == 1


@pytest.mark.parametrize("reference", ["drawing_price", "level_price"])
def test_book_compares_against_geometry_on_event_snapshot(reference):
    from talaria.foundation.key_levels import KeyLevel
    level = KeyLevel(level_id="support", instrument_id=INSTRUMENT, timeframe="1m", kind="SWING_LOW",
                     category="STRUCTURE", evidence_class="OBSERVED", price="100", strength=0.9,
                     status="ACTIVE", ts_available=BASE_TS, formed_ts=BASE_TS, method_version="test", touch_count=1, volume_weight="1")
    engine = AlertEngine(now_ms=Clock(BASE_TS + MINUTE),
                         key_level_provider=lambda i, t, cutoff: [level] if cutoff == BASE_TS + 1 else [])
    engine.refresh_drawings([_drawing()])
    operand = dict(source=reference, drawing_id="drw_alert") if reference == "drawing_price" else dict(
        source=reference, level_id="support", level_kind="SWING_LOW")
    alert = _rule_alert(dict(op="compare", test="gte", left=dict(source="event", metric="bid_price"),
                             right=operand), timing="event")
    engine.put(alert)
    for bid in ["99", "101"]:
        engine.handle_event(_book(bid, bid))
    assert alert.trigger_count == 1


def test_first_observed_cross_cannot_retrigger_an_already_true_disjunction():
    engine = AlertEngine(now_ms=Clock(BASE_TS + 2 * MINUTE))
    args = [_compare(dict(source="bar", field="close"), "100", "cross_above"),
            _compare(dict(source="bar", field="close"), "50")]
    alert = _rule_alert(dict(op="any", args=args))
    engine.seed_bars(INSTRUMENT, TIMEFRAME, [_bar(BASE_TS, "90")])
    engine.put(alert)
    engine.handle_event(_kline(BASE_TS + MINUTE, "110"))
    assert alert.trigger_count == 0


def test_migrated_snapshot_vwap_keeps_parent_observation_window():
    # 父提交在最近 600 根柱上计算 snapshot VWAP，窗口外高价不能压住后续上穿。
    from talaria.workbench_api.state_repositories.alert_migration import migrate_rule

    engine = AlertEngine(now_ms=Clock(BASE_TS + 602 * MINUTE))
    engine.seed_bars(INSTRUMENT, TIMEFRAME, [
        _bar(BASE_TS + i * MINUTE, "100000" if i == 0 else "10") for i in range(601)
    ])
    alert = _metric_alert(cooldown_ms=0)
    alert.condition = migrate_rule(dict(kind="INDICATOR_SIGNAL", indicator="VWAP", indicator_period=1,
                                        direction="ABOVE", threshold="11"), predates_pine=False)
    engine.put(alert)
    engine.handle_event(_kline(BASE_TS + 600 * MINUTE, "10"))
    engine.handle_event(_kline(BASE_TS + 601 * MINUTE, "1210"))
    assert alert.trigger_count == 1
