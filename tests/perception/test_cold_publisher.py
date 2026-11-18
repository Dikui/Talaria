"""冷库增量发布：只发布游标之后的新柱，排序去重后保持追加单调。

来源：任务单 06 的守护进程冷热一体验收与 lead review（m00382）。
测试只通过 ``ColdPublisher`` / ``ColdStore`` 的公开边界验证。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from talaria.foundation.connector import RawBar
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.perception.cold_publisher import ColdPublisher
from talaria.perception.cold_store import ColdBatch, ColdStore

pytestmark = [pytest.mark.medium, pytest.mark.spec]

INSTRUMENT = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
assert INSTRUMENT is not None
TIMEFRAME = "1m"
BASE = 1_763_596_800_000
MINUTE = 60_000


def _bar(ts: int, *, close: str = "100.5") -> RawBar:
    return RawBar(
        symbol="BTCUSDT",
        timeframe=TIMEFRAME,
        ts_event=ts,
        open=Decimal("100.0"),
        high=Decimal("101.0"),
        low=Decimal("99.0"),
        close=Decimal(close),
        volume=Decimal("1.0"),
        turnover=Decimal("100.5"),
        ts_available=ts + MINUTE,
        ts_ingested=ts + MINUTE,
        ts_committed=ts + MINUTE,
    )


def _bars(count: int = 3, *, start: int = BASE) -> list[RawBar]:
    return [_bar(start + index * MINUTE) for index in range(count)]


@pytest.fixture
def cold(tmp_path: Path):
    instance = ColdStore(tmp_path)
    yield instance
    instance.close()


def _publisher(cold: ColdStore) -> ColdPublisher:
    return ColdPublisher(cold, instrument=INSTRUMENT, timeframe=TIMEFRAME)


def test_publishes_only_bars_after_cursor(cold: ColdStore) -> None:
    cold.publish(ColdBatch(instrument=INSTRUMENT, timeframe=TIMEFRAME, bars=_bars(2)))

    generation = _publisher(cold).publish_snapshot_bars(_bars(3))

    assert generation is not None
    assert generation.first_ts == BASE + 2 * MINUTE
    assert generation.last_ts == BASE + 2 * MINUTE
    assert generation.bars == 1
    assert len(cold.generations(INSTRUMENT, TIMEFRAME)) == 2
    assert [bar.ts_event for bar in cold.read_bars(INSTRUMENT, TIMEFRAME)] == [
        BASE,
        BASE + MINUTE,
        BASE + 2 * MINUTE,
    ]


def test_returns_none_when_all_bars_are_at_or_before_cursor(cold: ColdStore) -> None:
    cold.publish(ColdBatch(instrument=INSTRUMENT, timeframe=TIMEFRAME, bars=_bars(2)))

    assert _publisher(cold).publish_snapshot_bars(_bars(2)) is None
    assert _publisher(cold).publish_snapshot_bars(_bars(1)) is None
    assert len(cold.generations(INSTRUMENT, TIMEFRAME)) == 1


def test_sorts_and_keeps_last_revision_per_timestamp(cold: ColdStore) -> None:
    revised = replace(_bar(BASE + 2 * MINUTE), close=Decimal("100.7"))

    generation = _publisher(cold).publish_snapshot_bars(
        [_bar(BASE + 2 * MINUTE), _bar(BASE), revised, _bar(BASE + MINUTE)]
    )

    assert generation is not None
    assert generation.bars == 3
    assert generation.first_ts == BASE
    assert generation.last_ts == BASE + 2 * MINUTE
    bars = cold.read_bars(INSTRUMENT, TIMEFRAME)
    assert [bar.ts_event for bar in bars] == [BASE, BASE + MINUTE, BASE + 2 * MINUTE]
    assert bars[-1].close == Decimal("100.7")


def test_drops_incomplete_bars(cold: ColdStore) -> None:
    incomplete = replace(_bar(BASE + 2 * MINUTE), close=None)

    generation = _publisher(cold).publish_snapshot_bars(
        [_bar(BASE), incomplete, _bar(BASE + MINUTE)]
    )

    assert generation is not None
    assert generation.bars == 2
    assert [bar.ts_event for bar in cold.read_bars(INSTRUMENT, TIMEFRAME)] == [
        BASE,
        BASE + MINUTE,
    ]


def test_returns_none_for_empty_input(cold: ColdStore) -> None:
    assert _publisher(cold).publish_snapshot_bars([]) is None
    assert cold.generations(INSTRUMENT, TIMEFRAME) == []
