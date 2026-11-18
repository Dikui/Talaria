"""BybitLinearConnector：请求参数、未闭合柱过滤、重试与错误映射。"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from talaria.foundation.derivatives import DerivativeKind
from talaria.foundation.errors import FeedUnavailableError
from talaria.packs.crypto_perp.connector import BybitLinearConnector

pytestmark = pytest.mark.small

NOW_MS = 1_793_600_000_000
MINUTE = 60_000


def _raw_payload(rows: list[list[str]], ret_code: int = 0) -> dict:
    return {
        "retCode": ret_code,
        "retMsg": "OK" if ret_code == 0 else "boom",
        "result": {"category": "linear", "symbol": "BTCUSDT", "list": rows},
        "time": NOW_MS,
    }


def _row(ts: int, price: str = "100.5") -> list[str]:
    return [str(ts), price, price, price, price, "1.5", "150.75"]


def _connector(handler, **kwargs) -> BybitLinearConnector:
    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.bybit.com"
    )
    return BybitLinearConnector(
        "https://api.bybit.com",
        client=client,
        retries=kwargs.pop("retries", 1),
        sleep=lambda _seconds: None,
        now_ms=lambda: NOW_MS,
        **kwargs,
    )


def test_request_parameters_and_ascending_decimal_bars() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        # Bybit 原始响应是降序；最后一根尚未收盘，必须被排除。
        rows = [
            _row(NOW_MS - MINUTE + 1),  # ts + interval > now
            _row(NOW_MS - 3 * MINUTE, "101.25"),
            _row(NOW_MS - 4 * MINUTE, "100.75"),
        ]
        return httpx.Response(200, json=_raw_payload(rows))

    connector = _connector(handler)
    bars = connector.fetch_bars(symbol="BTCUSDT", timeframe="1m", end_ts=NOW_MS, limit=2)

    assert len(requests) == 1
    params = requests[0].url.params
    assert str(requests[0].url).startswith("https://api.bybit.com/v5/market/kline")
    assert params["category"] == "linear"
    assert params["symbol"] == "BTCUSDT"
    assert params["interval"] == "1"
    assert params["limit"] == "2"
    assert params["end"] == str(NOW_MS)

    assert [bar.ts_event for bar in bars] == [NOW_MS - 4 * MINUTE, NOW_MS - 3 * MINUTE]
    assert bars[0].close == Decimal("100.75")
    assert bars[0].ts_available == bars[0].ts_event + MINUTE


def test_retries_transient_http_error_then_succeeds() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(503)
        return httpx.Response(
            200, json=_raw_payload([_row(NOW_MS - 2 * MINUTE, "100.5")])
        )

    bars = _connector(handler, retries=1).fetch_bars(symbol="BTCUSDT", timeframe="1m")
    assert attempts["count"] == 2
    assert len(bars) == 1


def test_persistent_http_error_maps_to_feed_unavailable() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(500)

    with pytest.raises(FeedUnavailableError):
        _connector(handler, retries=1).fetch_bars(symbol="BTCUSDT", timeframe="1m")
    assert attempts["count"] == 2


def test_ret_code_error_maps_to_feed_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_raw_payload([], ret_code=10006))

    with pytest.raises(FeedUnavailableError, match="10006"):
        _connector(handler).fetch_bars(symbol="BTCUSDT", timeframe="1m")


def test_malformed_row_maps_to_feed_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_raw_payload([["bad"]]))

    with pytest.raises(FeedUnavailableError):
        _connector(handler).fetch_bars(symbol="BTCUSDT", timeframe="1m")


def test_limit_is_capped_at_1000() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["limit"])
        return httpx.Response(200, json=_raw_payload([]))

    _connector(handler).fetch_bars(symbol="BTCUSDT", timeframe="1m", limit=5000)
    assert seen == ["1000"]


def test_unreachable_local_base_url_maps_to_feed_unavailable() -> None:
    """启动/请求路径不能被宿主代理环境干扰；连接失败应映射为 503 领域错误。"""
    connector = BybitLinearConnector("http://127.0.0.1:1", retries=0)
    try:
        with pytest.raises(FeedUnavailableError):
            connector.fetch_bars(symbol="BTCUSDT", timeframe="1m")
    finally:
        connector.close()


def _derivative_payload(rows: list[dict], **extra: object) -> dict:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {"category": "linear", "symbol": "BTCUSDT", "list": rows, **extra},
        "time": NOW_MS,
    }


def test_mark_price_backfill_uses_documented_endpoint_and_excludes_open_bar() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        rows = [
            [
                str(NOW_MS - MINUTE + 1),
                "101",
                "101",
                "101",
                "101",
            ],
            [
                str(NOW_MS - 2 * MINUTE),
                "100",
                "100",
                "100",
                "100.5",
            ],
        ]
        return httpx.Response(200, json=_derivative_payload(rows))

    points = _connector(handler).fetch_mark_price_bars(
        symbol="BTCUSDT", timeframe="1m", end_ts=NOW_MS, limit=2
    )

    params = requests[0].url.params
    assert str(requests[0].url).startswith("https://api.bybit.com/v5/market/mark-price-kline")
    assert params["category"] == "linear"
    assert params["symbol"] == "BTCUSDT"
    assert params["interval"] == "1"
    assert params["limit"] == "2"
    assert params["end"] == str(NOW_MS)
    assert [point.value for point in points] == [Decimal("100.5")]
    assert points[0].kind is DerivativeKind.MARK_PRICE
    assert points[0].ts_available == points[0].ts_event + MINUTE


def test_funding_history_uses_end_time_and_tracks_publication() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_derivative_payload(
                [
                    {
                        "symbol": "BTCUSDT",
                        "fundingRate": "0.0002",
                        "fundingRateTimestamp": str(NOW_MS - 8 * 3_600_000),
                    },
                    {
                        "symbol": "BTCUSDT",
                        "fundingRate": "0.0001",
                        "fundingRateTimestamp": str(NOW_MS - 16 * 3_600_000),
                    },
                ]
            ),
        )

    points = _connector(handler).fetch_funding_settlements(
        symbol="BTCUSDT", end_ts=NOW_MS, limit=10
    )

    params = requests[0].url.params
    assert str(requests[0].url).startswith("https://api.bybit.com/v5/market/funding/history")
    assert params["endTime"] == str(NOW_MS)
    assert params["limit"] == "10"
    assert [point.value for point in points] == [Decimal("0.0001"), Decimal("0.0002")]
    assert points[-1].ts_published == points[-1].ts_event
    assert points[-1].ts_available == points[-1].ts_event


def test_open_interest_uses_interval_time_and_excludes_open_point() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_derivative_payload(
                [
                    {"openInterest": "999", "timestamp": str(NOW_MS - MINUTE + 1)},
                    {"openInterest": "123.5", "timestamp": str(NOW_MS - 10 * MINUTE)},
                ]
            ),
        )

    points = _connector(handler).fetch_open_interest(
        symbol="BTCUSDT", timeframe="5m", end_ts=NOW_MS, limit=10
    )

    params = requests[0].url.params
    assert str(requests[0].url).startswith("https://api.bybit.com/v5/market/open-interest")
    assert params["intervalTime"] == "5min"
    assert params["endTime"] == str(NOW_MS)
    assert [point.value for point in points] == [Decimal("123.5")]
    assert points[0].ts_available == points[0].ts_event + 5 * MINUTE


def test_unknown_open_interest_timeframe_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_derivative_payload([]))

    with pytest.raises(FeedUnavailableError):
        _connector(handler).fetch_open_interest(symbol="BTCUSDT", timeframe="3m")
