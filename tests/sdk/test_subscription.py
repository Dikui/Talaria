"""Unit tests for SDK strategy subscription resync helpers."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from talaria.foundation.strategy_subscription import SubscriptionRequest
from talaria.sdk._subscription import _range_revisions
from talaria.service_endpoint import DATA_DIR_HEADER

pytestmark = [pytest.mark.small, pytest.mark.regression]

INSTRUMENT = "BTC-USDT-PERP"
MINUTE = 60_000
START = 1_793_491_200_000  # 2026-11-01T00:00:00Z


def test_range_revisions_skips_http_when_resuming_within_same_minute(monkeypatch) -> None:
    """Crash recovery within the same minute as `resume_from` must not send start > end (422)."""
    called = False

    async def unexpected_get(self, *args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError(f"Unexpected HTTP GET during same-minute resync: {args} {kwargs}")

    monkeypatch.setattr(httpx.AsyncClient, "get", unexpected_get)
    req = SubscriptionRequest(instrument=INSTRUMENT, timeframe="1m", owner="test-owner")

    # `now` is 30s into START minute -> last_closed_bar_start(now, 1m) == START - MINUTE < range_start.
    result = asyncio.run(
        _range_revisions(
            "http://127.0.0.1:8765",
            "dir-id",
            req,
            coverage_start=START,
            now=START + 30_000,
            interval=MINUTE,
            seen=set(),
            range_start=START,
        )
    )

    assert result == []
    assert not called


def test_range_revisions_fetches_once_resume_minute_closes(monkeypatch) -> None:
    """Once `now` crosses the minute boundary (`end == range_start`), resync queries `/api/bars`."""
    requested: list[dict[str, object]] = []

    async def fake_get(self, url, *, params=None, **kwargs):
        requested.append({"url": url, "params": dict(params or {})})
        return httpx.Response(
            200,
            request=httpx.Request("GET", f"http://127.0.0.1:8765{url}", params=params),
            headers={DATA_DIR_HEADER: "dir-id"},
            json={
                "instrument_id": INSTRUMENT,
                "timeframe": "1m",
                "generation": 0,
                "bars": [
                    {
                        "ts_event": START,
                        "open": "100",
                        "high": "101",
                        "low": "99",
                        "close": "100.5",
                        "volume": "1",
                        "turnover": "100.5",
                        "ts_available": START + MINUTE,
                        "precision": "SECOND_VERIFIED",
                        "confirmed": True,
                        "revision": 0,
                    }
                ],
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    req = SubscriptionRequest(instrument=INSTRUMENT, timeframe="1m", owner="test-owner")

    result = asyncio.run(
        _range_revisions(
            "http://127.0.0.1:8765",
            "dir-id",
            req,
            coverage_start=START,
            now=START + MINUTE,
            interval=MINUTE,
            seen=set(),
            range_start=START,
        )
    )

    assert len(requested) == 1
    assert requested[0]["url"] == "/api/bars"
    assert requested[0]["params"]["start"] == START
    assert requested[0]["params"]["end"] == START
    assert len(result) == 1
    assert result[0][1].ts_event == START
