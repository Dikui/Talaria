"""JSON 规则契约（alerts-and-scanning/01，ADR-0019）。"""

import pytest
from pydantic import ValidationError

from talaria.foundation.alerts import AlertCreate

pytestmark = [pytest.mark.small, pytest.mark.spec]


def test_alert_accepts_a_versioned_composite_rule() -> None:
    rule = {
        "version": "1", "timing": "bar_close",
        "expression": {"op": "all", "args": [
            {"op": "compare", "test": "cross_above",
             "left": {"source": "bar", "field": "close"},
             "right": {"source": "indicator", "name": "EMA", "params": {"period": 20}}},
            {"op": "compare", "test": "lt",
             "left": {"source": "indicator", "name": "RSI"},
             "right": {"source": "constant", "value": "70"}},
            {"op": "compare", "test": "lt",
             "left": {"source": "derivative", "kind": "funding_rate"},
             "right": {"source": "constant", "value": "0"}},
        ]},
    }
    payload = AlertCreate.model_validate({"instrument_id": "BTC", "timeframe": "5m", "condition": rule})
    assert payload.condition.model_dump(mode="json", exclude_unset=True) == rule


@pytest.mark.parametrize("version, timing, name, extra, message", [
    ("2", "event", "EMA", {}, "version"),
    ("1", "event", "UNREGISTERED", {}, "name"),
    ("1", "event", "EMA", {"version": "2"}, "version"),
    ("1", "event", "EMA", {"confirmed": False}, "confirmed"),
])
def test_rule_rejects_an_incompatible_version_or_unsafe_operator(
    version, timing, name, extra, message,
) -> None:
    with pytest.raises(ValidationError, match=message):
        AlertCreate.model_validate({
            "instrument_id": "BTC", "timeframe": "1m", "condition": {
                "version": version, "timing": timing, "expression": {
                    "op": "compare", "test": "gte",
                    "left": {"source": "event", "metric": "bid_price"},
                    "right": {"source": "indicator", "name": name, **extra},
                },
            },
        })


def test_event_comparison_uses_only_the_latest_confirmed_operator_output() -> None:
    from decimal import Decimal

    from talaria.components import Ctx, compile_rule
    from talaria.foundation.bars import RangeBar
    from talaria.foundation.feed import OrderBookEvent
    from talaria.foundation.rules import Rule
    from talaria.foundation.types import PrecisionTier

    rule = Rule.model_validate({"version": "1", "timing": "event", "expression": {
        "op": "compare", "test": "gt", "left": {"source": "event", "metric": "bid_price"},
        "right": {"source": "indicator", "name": "EMA", "params": {"period": 1}},
    }})

    def bar(ts, price, confirmed):
        return RangeBar(
            ts_event=ts, open=price, high=price, low=price, close=price, volume="1",
            turnover=price, ts_available=60_000, precision=PrecisionTier.SECOND_VERIFIED,
            confirmed=confirmed, revision=0,
        )

    ctx = Ctx(
        "BTC", "1m", 0, 70_000,
        _read_bars=lambda *args, **kwargs: [bar(0, "100", True), bar(60_000, "200", False)],
        _indicator_defaults={},
    )
    event = OrderBookEvent(
        instrument_id="BTC", bid_price="120", bid_size="1", ask_price="121", ask_size="1",
        ts_event=70_000, ts_ingested=70_000,
    )
    compiled = compile_rule(rule)
    assert compiled.evaluate(ctx) == []
    match = compiled.observe(ctx, event=event)
    assert match is not None and match.matched
    assert (match.observed, match.threshold) == (Decimal("120"), Decimal("100"))
