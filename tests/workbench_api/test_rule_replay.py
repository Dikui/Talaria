"""alerts-and-scanning/02. Replay the frozen inputs through the alert state machine."""
from decimal import Decimal

import pytest

from talaria.foundation.bars import CompactBar
from talaria.foundation.feed import ReceivedKlineEvent
from talaria.foundation.rules import RulePreviewRequest
from talaria.workbench_api.rule_preview import evaluate_preview

pytestmark = pytest.mark.medium
MINUTE = 60_000
SYMBOL = "BTC-USDT-PERP"


def bar(slot, price, *, available=None):
    return CompactBar(timestamp=slot * MINUTE, ts_available=available or (slot + 1) * MINUTE,
                      precision="SECOND_VERIFIED", status="NORMAL", open=price, high=price, low=price,
                      close=price, volume="1", turnover=price)


def event(slot, price, received, revision=0):
    return ReceivedKlineEvent(instrument_id=SYMBOL, timeframe="1m", confirmed=True,
                              revision=revision, ts_ingested=received, bar=bar(slot, price))


def request(left=None, right=None, **options):
    return RulePreviewRequest.model_validate({
        "timeframe": "1m", "limit": 1000, **options,
        "rule": {"version": "1", "timing": "bar_close", "expression": {
            "op": "compare", "test": "gt", "left": left or {"source": "bar", "field": "close"},
            "right": right or {"source": "constant", "value": "10"},
        }},
    })


def test_replay_observes_true_bars_and_actual_cooldown_edges_separately():
    bars = [bar(i, p) for i, p in enumerate(["5", "15", "16", "17", "4", "14"])]
    preview = evaluate_preview(request(), SYMBOL, 6 * MINUTE, bars, lambda: (), {}, lambda _: ())
    assert [hit.timestamp for hit in preview.condition_true] == [MINUTE, 2 * MINUTE, 3 * MINUTE, 5 * MINUTE]
    assert [(hit.timestamp, hit.observed_at) for hit in preview.would_trigger] == [
        (MINUTE, 2 * MINUTE), (5 * MINUTE, 6 * MINUTE),
    ]


def test_late_first_confirmation_is_skipped_and_revision_reobserves_latest_slot():
    events = [event(0, "1", MINUTE), event(2, "9", 3 * MINUTE),
              event(1, "55", 3 * MINUTE + 10), event(0, "2", 3 * MINUTE + 20, revision=1)]
    req = request({"source": "indicator", "name": "SMA", "params": {"period": 2}})
    result = evaluate_preview(req, SYMBOL, 4 * MINUTE, [e.bar for e in events], lambda: (), {}, lambda _: (),
                              events=events)
    assert [(hit.timestamp, hit.price, hit.observed_at) for hit in result.would_trigger] == [
        (2 * MINUTE, Decimal(9), 3 * MINUTE + 20),
    ]
    assert [hit.timestamp for hit in result.condition_true] == [2 * MINUTE]


def test_drawing_revision_between_close_and_receipt_is_visible():
    from talaria.foundation.drawings import DrawingRecord

    versions = [DrawingRecord(drawing_id="line", instrument_id=SYMBOL, kind="HORIZONTAL_LINE",
                              visible_timeframes=[], points=[{"ts": 0, "price": price}], text="", style={},
                              version=i, updated_ts=ts, created_ts=0)
                for i, ts, price in [(1, 0, "100"), (2, MINUTE + 10, "5")]]
    late = event(0, "10", MINUTE + 20)
    result = evaluate_preview(request(right={"source": "drawing_price", "drawing_id": "line"}),
                              SYMBOL, 2 * MINUTE, [late.bar], lambda: (), {"line": versions}, lambda _: (),
                              events=[late])
    assert [(hit.timestamp, hit.observed_at) for hit in result.condition_true] == [(0, MINUTE + 20)]


def test_chart_anchored_vwap_keeps_first_bar_beyond_migrated_window():
    bars = [bar(0, "602"), *(bar(i, "1") for i in range(1, 601))]
    result = evaluate_preview(request({"source": "indicator", "name": "VWAP", "window": "chart",
                                       "params": {"anchor": "snapshot"}}, limit=1),
                              SYMBOL, 601 * MINUTE, bars, lambda: (), {}, lambda *_: ())
    assert result.indicators[0].points[-1].values["value"] == Decimal(2)


def test_preview_1000_slots_four_boll_conditions_under_two_seconds():
    from time import perf_counter

    bars = [bar(i, str(100 + i % 11)) for i in range(1000)]
    req = request()
    args = []
    for period, channel in [(20, "mid"), (20, "upper"), (20, "lower"), (50, "mid")]:
        args.append({"op": "compare", "test": "gt",
                     "left": {"source": "indicator", "name": "BOLL", "channel": channel, "params": {"period": period}},
                     "right": {"source": "constant", "value": "0"}})
    req.rule = req.rule.model_validate({**req.rule.model_dump(), "expression": {"op": "all", "args": args}})
    start = perf_counter()
    result = evaluate_preview(req, SYMBOL, 1000 * MINUTE, bars, lambda: (), {}, lambda *_: ())
    elapsed = perf_counter() - start
    assert len(result.condition_true) == 951
    assert elapsed < 2, elapsed


def test_early_confirmed_packet_cannot_see_drawing_published_before_close_but_after_receipt():
    from talaria.foundation.drawings import DrawingRecord

    line = DrawingRecord(drawing_id="new", instrument_id=SYMBOL, kind="HORIZONTAL_LINE",
                         visible_timeframes=[], points=[{"ts": 0, "price": "1"}],
                         text="", style={}, version=1, updated_ts=MINUTE - 1, created_ts=MINUTE - 1)
    early = event(0, "10", MINUTE - 2)
    result = evaluate_preview(request(right={"source": "drawing_price", "drawing_id": "new"}),
                              SYMBOL, MINUTE, [early.bar], lambda: (), {"new": [line]}, lambda *_: (),
                              events=[early])
    assert result.condition_true == []


def test_recovery_establishes_a_baseline_before_rearming():
    from talaria.foundation.feed import FeedHealthEvent

    events = [event(0, "5", MINUTE),
              FeedHealthEvent(instrument_id=SYMBOL, timeframe="1m", status="STALE", ts=MINUTE+1, stale_since=MINUTE),
              FeedHealthEvent(instrument_id=SYMBOL, timeframe="1m", status="LIVE", ts=MINUTE+2),
              event(1, "11", 2*MINUTE), event(2, "5", 3*MINUTE), event(3, "12", 4*MINUTE)]
    result = evaluate_preview(request(), SYMBOL, 4 * MINUTE, [e.bar for e in events if hasattr(e, "bar")],
                              lambda: (), {}, lambda *_: (), events=events)
    assert [hit.timestamp for hit in result.condition_true] == [MINUTE, 3 * MINUTE]
    assert [hit.observed_at for hit in result.would_trigger] == [4 * MINUTE]


def test_same_millisecond_ticker_after_close_does_not_supply_the_earlier_close():
    from talaria.foundation.feed import DerivativeTickEvent
    from talaria.perception.snapshot_events import SnapshotEvent

    tick = DerivativeTickEvent(instrument_id=SYMBOL, ts_ingested=MINUTE, ts_event=MINUTE, mark_price="20")
    events = [SnapshotEvent(1, event(0, "10", MINUTE)), SnapshotEvent(2, tick),
              SnapshotEvent(3, event(1, "10", 2 * MINUTE))]
    result = evaluate_preview(request({"source": "derivative", "kind": "mark_price"}), SYMBOL,
                              2 * MINUTE, [bar(0, "10"), bar(1, "10")], lambda: list(tick.points()),
                              {}, lambda *_: (), events=events)
    assert [hit.timestamp for hit in result.condition_true] == [MINUTE]


def test_incremental_rule_indicator_uses_the_chart_decimal_context():
    from decimal import localcontext
    from talaria.foundation.operators import compute_series, spec_from_params

    bars = [bar(i, str(10 + i)) for i in range(9)]
    req = request({"source": "indicator", "name": "EMA", "window": "chart", "params": {"period": 6}})
    expected = compute_series(bars, spec_from_params("EMA", {"period": 6})).points
    with localcontext(prec=6):
        result = evaluate_preview(req, SYMBOL, 9 * MINUTE, bars, lambda: (), {}, lambda *_: ())
    assert [point.values for point in result.indicators[0].points] == list(expected)
