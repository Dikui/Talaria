"""L0 统一实时事件契约：K 线事件、盘口事件与供流健康。

来源：任务单 03 与 docs/architecture.md §3.2。实时流、回放与后续 Scanner 共用
这一组契约，因此十进制字段必须跨 JSON 保持字符串，健康状态只允许三个值。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.feed import (
    FeedHealthEvent,
    FeedHealthReport,
    FeedHealthStatus,
    KlineEvent,
    OrderBookEvent,
)
from talaria.foundation.types import PrecisionTier

pytestmark = pytest.mark.small


def _bar(**overrides) -> CompactBar:
    payload = {
        "timestamp": 1_700_000_060_000,
        "open": "101.10",
        "high": "102.20",
        "low": "100.00",
        "close": "101.50",
        "volume": "12.345",
        "turnover": "1250.75",
        "status": BarStatus.NORMAL,
        "ts_available": 1_700_000_120_000,
        "precision": PrecisionTier.SECOND_VERIFIED,
    }
    payload.update(overrides)
    return CompactBar(**payload)


def _event(**overrides) -> KlineEvent:
    payload = {
        "instrument_id": "BTC-USDT-PERP",
        "timeframe": "1m",
        "bar": _bar(),
        "confirmed": False,
        "revision": 2,
    }
    payload.update(overrides)
    return KlineEvent(**payload)


def _book(**overrides) -> OrderBookEvent:
    payload = {
        "instrument_id": "BTC-USDT-PERP",
        "bid_price": "101.40",
        "bid_size": "0.750",
        "ask_price": "101.50",
        "ask_size": "1.250",
        "ts_event": 1_700_000_060_500,
        "ts_ingested": 1_700_000_060_520,
    }
    payload.update(overrides)
    return OrderBookEvent(**payload)


def test_kline_event_keeps_decimal_fields_as_strings_across_json() -> None:
    payload = _event().model_dump(mode="json")

    assert payload["bar"]["open"] == "101.10"
    assert payload["bar"]["close"] == "101.50"
    assert payload["bar"]["volume"] == "12.345"
    assert payload["confirmed"] is False
    assert payload["revision"] == 2


def test_kline_event_revision_is_non_negative() -> None:
    assert _event(revision=0).revision == 0
    with pytest.raises(ValidationError):
        _event(revision=-1)


def test_kline_event_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _event(symbol="BTCUSDT")


def test_order_book_event_serializes_prices_and_sizes_as_strings() -> None:
    payload = _book().model_dump(mode="json")

    assert payload["bid_price"] == "101.40"
    assert payload["ask_size"] == "1.250"
    assert payload["ts_event"] == 1_700_000_060_500


def test_order_book_event_rejects_crossed_book() -> None:
    with pytest.raises(ValidationError, match="买价不能高于卖价"):
        _book(bid_price="101.60")


def test_feed_health_status_only_allows_three_states() -> None:
    assert [status.value for status in FeedHealthStatus] == [
        "LIVE",
        "STALE",
        "RECOVERING",
    ]
    with pytest.raises(ValidationError):
        FeedHealthEvent(
            instrument_id="BTC-USDT-PERP",
            timeframe="1m",
            status="UNKNOWN",
            ts=1_700_000_060_000,
            last_event_ts=None,
            stale_since=None,
            reconnect_count=0,
        )


def test_feed_health_event_requires_stale_since_when_stale() -> None:
    stale = FeedHealthEvent(
        instrument_id="BTC-USDT-PERP",
        timeframe="1m",
        status=FeedHealthStatus.STALE,
        ts=1_700_000_072_000,
        last_event_ts=1_700_000_060_000,
        stale_since=1_700_000_060_000,
        reconnect_count=1,
    )
    assert stale.status is FeedHealthStatus.STALE

    with pytest.raises(ValidationError, match="STALE 必须记录断流起点"):
        FeedHealthEvent(
            instrument_id="BTC-USDT-PERP",
            timeframe="1m",
            status=FeedHealthStatus.STALE,
            ts=1_700_000_072_000,
            last_event_ts=None,
            stale_since=None,
            reconnect_count=1,
        )


def test_live_or_recovering_health_must_clear_stale_since() -> None:
    with pytest.raises(ValidationError, match="LIVE 不能带断流起点"):
        FeedHealthEvent(
            instrument_id="BTC-USDT-PERP",
            timeframe="1m",
            status=FeedHealthStatus.LIVE,
            ts=1_700_000_080_000,
            last_event_ts=1_700_000_080_000,
            stale_since=1_700_000_060_000,
            reconnect_count=1,
        )


def test_feed_health_report_lists_channels_and_aggregate_status() -> None:
    live = FeedHealthEvent(
        instrument_id="BTC-USDT-PERP",
        timeframe="1m",
        status=FeedHealthStatus.LIVE,
        ts=1_700_000_080_000,
        last_event_ts=1_700_000_079_900,
        stale_since=None,
        reconnect_count=0,
    )
    report = FeedHealthReport(
        feed_mode="replay",
        status=FeedHealthStatus.LIVE,
        channels=[live],
    )
    payload = report.model_dump(mode="json")

    assert payload["feed_mode"] == "replay"
    assert payload["status"] == "LIVE"
    assert payload["channels"][0]["instrument_id"] == "BTC-USDT-PERP"
    assert payload["channels"][0]["timeframe"] == "1m"
