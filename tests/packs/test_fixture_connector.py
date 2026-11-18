"""离线 FixtureConnector：同一协议、按 end 向左分页、错误映射。"""

from __future__ import annotations

import pytest

from talaria.foundation.errors import FeedUnavailableError
from talaria.packs.testing import FixtureConnector

pytestmark = pytest.mark.small

MINUTE = 60_000


def test_latest_page_is_ascending_and_limited(fixture_dir) -> None:
    connector = FixtureConnector(fixture_dir)
    bars = connector.fetch_bars(symbol="BTCUSDT", timeframe="1m", limit=300)
    assert len(bars) == 300
    assert [bar.ts_event for bar in bars] == sorted(bar.ts_event for bar in bars)
    assert bars[-1].ts_event == 1_793_545_440_000
    assert bars[-1].ts_available == bars[-1].ts_event + MINUTE


def test_end_before_oldest_uses_strictly_older_page(fixture_dir) -> None:
    connector = FixtureConnector(fixture_dir)
    latest = connector.fetch_bars(symbol="BTCUSDT", timeframe="1m", limit=300)
    older = connector.fetch_bars(
        symbol="BTCUSDT",
        timeframe="1m",
        end_ts=latest[0].ts_event - 1,
        limit=300,
    )
    assert len(older) == 300
    assert older[-1].ts_event < latest[0].ts_event
    assert {bar.ts_event for bar in older}.isdisjoint({bar.ts_event for bar in latest})


def test_three_pages_cover_nine_hundred_slots_without_duplicates(fixture_dir) -> None:
    connector = FixtureConnector(fixture_dir)
    pages = []
    end_ts = None
    for _ in range(3):
        page = connector.fetch_bars(symbol="BTCUSDT", timeframe="1m", end_ts=end_ts, limit=300)
        pages.append(page)
        end_ts = page[0].ts_event - 1
    assert all(
        [bar.ts_event for bar in page] == sorted(bar.ts_event for bar in page)
        for page in pages
    )
    timestamps = [bar.ts_event for page in pages for bar in page]
    # fixture 原始响应有 900 根；插入的 5 根 FEED_GAP 属于紧凑化层。
    assert len(timestamps) == 900
    assert len(set(timestamps)) == 900


def test_missing_fixture_maps_to_feed_unavailable(tmp_path) -> None:
    with pytest.raises(FeedUnavailableError):
        FixtureConnector(tmp_path).fetch_bars(symbol="BTCUSDT", timeframe="1m")


def test_malformed_fixture_maps_to_feed_unavailable(tmp_path) -> None:
    path = tmp_path / "BTCUSDT" / "1m.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(FeedUnavailableError):
        FixtureConnector(tmp_path).fetch_bars(symbol="BTCUSDT", timeframe="1m")


def test_zero_limit_returns_empty(fixture_dir) -> None:
    assert FixtureConnector(fixture_dir).fetch_bars(symbol="BTCUSDT", timeframe="1m", limit=0) == []
