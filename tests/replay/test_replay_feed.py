"""回放传输：偏移节奏、断流/恢复段与输入校验。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from talaria.foundation.errors import FeedTransportError, InvalidRequestError
from talaria.foundation.feed import KlineEvent, OrderBookEvent
from talaria.replay import ReplayFeed

pytestmark = [pytest.mark.small, pytest.mark.spec]

BASE = 1_763_596_800_000
MINUTE = 60_000
INSTRUMENT = "BTC-USDT-PERP"


def compact(timestamp: int, close: str, *, confirmed: bool) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": "1",
        "turnover": "1",
        "status": "NORMAL",
        "ts_available": timestamp + MINUTE,
        "precision": "SECOND_VERIFIED",
    }


def kline_line(offset_ms: int, timestamp: int, close: str, *, confirmed: bool) -> dict:
    return {
        "offset_ms": offset_ms,
        "type": "kline",
        "event": {
            "instrument_id": INSTRUMENT,
            "timeframe": "1m",
            "bar": compact(timestamp, close, confirmed=confirmed),
            "confirmed": confirmed,
            "revision": 0,
        },
    }


def book_line(offset_ms: int, *, instrument_id: str = INSTRUMENT) -> dict:
    return {
        "offset_ms": offset_ms,
        "type": "book",
        "event": {
            "instrument_id": instrument_id,
            "bid_price": "100.0",
            "bid_size": "2",
            "ask_price": "100.1",
            "ask_size": "3",
            "ts_event": BASE + offset_ms,
            "ts_ingested": BASE + offset_ms,
        },
    }


def write_replay(tmp_path: Path, lines: list[dict]) -> Path:
    path = tmp_path / "replay.jsonl"
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )
    return path


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_events_follow_offsets_and_speed(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = write_replay(
            tmp_path,
            [
                kline_line(0, BASE, "100", confirmed=False),
                book_line(1_000),
                kline_line(2_500, BASE, "101", confirmed=True),
            ],
        )
        fake = FakeTime()
        feed = ReplayFeed(path, speed=2.0, sleep=fake.sleep, clock=fake.clock)
        await feed.connect()
        await feed.subscribe(instrument_id=INSTRUMENT, timeframe="1m")

        first = await feed.recv()
        second = await feed.recv()
        third = await feed.recv()

        assert isinstance(first, KlineEvent) and first.bar.close == 100
        assert isinstance(second, OrderBookEvent)
        assert isinstance(third, KlineEvent) and third.confirmed is True
        assert fake.sleeps == [0.5, 0.75]

    asyncio.run(scenario())


def test_disconnect_raises_and_connect_waits_for_reconnect_offset(tmp_path: Path) -> None:
    path = write_replay(
        tmp_path,
        [
            kline_line(0, BASE, "100", confirmed=False),
            {"offset_ms": 1_000, "type": "disconnect", "reason": "样本断流"},
            {"offset_ms": 4_000, "type": "reconnect"},
            kline_line(5_000, BASE, "101", confirmed=True),
        ],
    )

    async def scenario() -> None:
        fake = FakeTime()
        feed = ReplayFeed(path, sleep=fake.sleep, clock=fake.clock)
        await feed.connect()
        await feed.subscribe(instrument_id=INSTRUMENT, timeframe="1m")
        await feed.recv()
        with pytest.raises(FeedTransportError, match="样本断流"):
            await feed.recv()
        assert fake.now == pytest.approx(1.0)

        await feed.connect()
        assert fake.now == pytest.approx(4.0)
        resumed = await feed.recv()
        assert isinstance(resumed, KlineEvent) and resumed.bar.close == 101
        assert fake.now == pytest.approx(5.0)

    asyncio.run(scenario())


def test_unsubscribed_channels_are_skipped(tmp_path: Path) -> None:
    path = write_replay(
        tmp_path,
        [
            book_line(0, instrument_id="ETH-USDT-PERP"),
            kline_line(0, BASE, "100", confirmed=False),
            book_line(0),
        ],
    )

    async def scenario() -> None:
        fake = FakeTime()
        feed = ReplayFeed(path, sleep=fake.sleep, clock=fake.clock)
        await feed.connect()
        await feed.subscribe(instrument_id=INSTRUMENT, timeframe="1m")
        event = await feed.recv()
        assert isinstance(event, KlineEvent)
        follow = await feed.recv()
        assert isinstance(follow, OrderBookEvent)

    asyncio.run(scenario())


def test_idle_heartbeat_after_last_line(tmp_path: Path) -> None:
    path = write_replay(tmp_path, [kline_line(0, BASE, "100", confirmed=False)])

    async def scenario() -> None:
        fake = FakeTime()
        feed = ReplayFeed(path, speed=2.0, sleep=fake.sleep, clock=fake.clock)
        await feed.connect()
        await feed.subscribe(instrument_id=INSTRUMENT, timeframe="1m")
        await feed.recv()
        assert await feed.recv() is None
        assert fake.sleeps == [2.5]
        await feed.close()
        with pytest.raises(FeedTransportError):
            await feed.recv()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ([], "为空"),
        ([{"offset_ms": 0, "type": "tick"}], "type 不合法"),
        ([{"type": "heartbeat"}], "offset_ms"),
        (
            [
                {"offset_ms": 100, "type": "heartbeat"},
                {"offset_ms": 50, "type": "heartbeat"},
            ],
            "逆序",
        ),
    ],
)
def test_malformed_replay_rejected(tmp_path: Path, lines: list[dict], message: str) -> None:
    path = write_replay(tmp_path, lines)
    with pytest.raises(InvalidRequestError, match=message):
        ReplayFeed(path)


def test_invalid_speed_rejected(tmp_path: Path) -> None:
    path = write_replay(tmp_path, [{"offset_ms": 0, "type": "heartbeat"}])
    with pytest.raises(InvalidRequestError, match="速度"):
        ReplayFeed(path, speed=0)
