"""Feed Supervisor：修订、NO_TRADES / FEED_GAP、断流健康与回填幂等。

来源：任务单 03 与 docs/architecture.md §3.2。测试只通过 ``MarketEventFeed``
的公开方法驱动状态机，并注入固定时钟，不产生任何真实等待。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from talaria.feed.supervisor import FeedSupervisor
from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.connector import RawBar
from talaria.foundation.errors import (
    FeedTransportError,
    InstrumentNotFoundError,
    InvalidRequestError,
)
from talaria.foundation.feed import (
    DerivativeTickEvent,
    FeedHealthEvent,
    FeedHealthStatus,
    KlineEvent,
    OrderBookEvent,
)
from talaria.foundation.types import PrecisionTier
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK

pytestmark = [pytest.mark.small, pytest.mark.spec]

#: 2025-11-20T00:00:00Z，对齐到整分钟。
BASE = 1_763_596_800_000
MINUTE = 60_000
INSTRUMENT = "BTC-USDT-PERP"
SYMBOL = "BTCUSDT"


class Clock:
    def __init__(self, now: int = BASE) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def set(self, now: int) -> int:
        self.now = now
        return now

    def advance(self, ms: int) -> int:
        self.now += ms
        return self.now


class StubStream:
    """同步测试不连接传输层，只用于让 supervisor 拿到 feed_mode。"""

    feed_mode = "stub"

    async def connect(self) -> None: ...

    async def subscribe(self, *, instrument_id: str, timeframe: str) -> None: ...

    async def unsubscribe(self, *, instrument_id: str, timeframe: str) -> None: ...

    async def recv(self) -> KlineEvent | OrderBookEvent | None:
        raise FeedTransportError("测试替身不提供数据")

    async def close(self) -> None: ...


class StubConnector:
    def __init__(self, bars: list[RawBar] | None = None) -> None:
        self.bars = list(bars or [])
        self.calls: list[dict[str, object]] = []

    def fetch_bars(self, *, symbol: str, timeframe: str, end_ts: int | None = None, limit: int = 1000):
        self.calls.append(
            {"symbol": symbol, "timeframe": timeframe, "end_ts": end_ts, "limit": limit}
        )
        return list(self.bars)


def ts(index: int) -> int:
    return BASE + index * MINUTE


def compact_bar(
    timestamp: int,
    close: str,
    *,
    volume: str = "1.5",
    status: BarStatus = BarStatus.NORMAL,
    confirmed: bool = False,
    open_: str | None = None,
) -> CompactBar:
    price = Decimal(close)
    opening = Decimal(open_) if open_ is not None else price
    return CompactBar(
        timestamp=timestamp,
        open=opening,
        high=max(opening, price),
        low=min(opening, price),
        close=price,
        volume=Decimal(volume),
        turnover=Decimal("100"),
        status=status,
        ts_available=timestamp + (MINUTE if confirmed else 1_000),
        precision=PrecisionTier.SECOND_VERIFIED,
    )


def kline(
    timestamp: int,
    close: str,
    *,
    confirmed: bool = False,
    open_: str | None = None,
) -> KlineEvent:
    return KlineEvent(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bar=compact_bar(timestamp, close, confirmed=confirmed, open_=open_),
        confirmed=confirmed,
        revision=0,
    )


def book(
    *,
    instrument_id: str = INSTRUMENT,
    bid: str = "100.0",
    ask: str = "100.1",
    now: int = BASE,
) -> OrderBookEvent:
    return OrderBookEvent(
        instrument_id=instrument_id,
        bid_price=bid,
        bid_size="2.5",
        ask_price=ask,
        ask_size="3.5",
        ts_event=now,
        ts_ingested=now,
    )


def raw(timestamp: int, close: str, *, volume: str = "2") -> RawBar:
    price = Decimal(close)
    return RawBar(
        symbol=SYMBOL,
        timeframe="1m",
        ts_event=timestamp,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal(volume),
        turnover=Decimal("200"),
        ts_available=timestamp + MINUTE,
        ts_ingested=timestamp + MINUTE,
        ts_committed=timestamp + MINUTE,
    )


def make(
    clock: Clock | None = None,
    connector: StubConnector | None = None,
    *,
    subscribe: bool = True,
) -> tuple[FeedSupervisor, list[object]]:
    clock = clock or Clock()
    supervisor = FeedSupervisor(
        stream=StubStream(),
        connector=connector or StubConnector(),
        instruments=CRYPTO_PERP_PACK.instruments(),
        now_ms=clock,
        stale_after_ms=12_000,
    )
    events: list[object] = []
    supervisor.add_listener(events.append)
    if subscribe:
        supervisor.subscribe(instrument_id=INSTRUMENT, timeframe="1m")
    return supervisor, events


def klines(events: list[object]) -> list[KlineEvent]:
    return [event for event in events if isinstance(event, KlineEvent)]


def healths(events: list[object]) -> list[FeedHealthEvent]:
    return [event for event in events if isinstance(event, FeedHealthEvent)]


def test_in_progress_updates_and_confirmation_increment_revision() -> None:
    clock = Clock(BASE + 1_000)
    supervisor, events = make(clock)

    supervisor.ingest(kline(ts(0), "100"), now=clock())
    supervisor.ingest(kline(ts(0), "101"), now=clock.advance(500))
    supervisor.ingest(kline(ts(0), "101", confirmed=True), now=clock.advance(500))

    bars = klines(events)
    assert [event.revision for event in bars] == [0, 1, 2]
    assert [event.confirmed for event in bars] == [False, False, True]
    assert {event.bar.timestamp for event in bars} == {ts(0)}


def test_identical_republish_is_idempotent() -> None:
    clock = Clock(BASE + MINUTE + 1_000)
    supervisor, events = make(clock)

    supervisor.ingest(kline(ts(0), "100", confirmed=True), now=clock())
    supervisor.ingest(kline(ts(0), "100", confirmed=True), now=clock.advance(100))

    assert len(klines(events)) == 1


def test_revision_of_same_timestamp_takes_last_write() -> None:
    clock = Clock(BASE + 1_000)
    supervisor, events = make(clock)

    supervisor.ingest(kline(ts(0), "100", confirmed=True), now=clock())
    supervisor.ingest(kline(ts(0), "100.5", confirmed=True), now=clock.advance(10))

    bars = klines(events)
    assert [event.revision for event in bars] == [0, 1]
    assert bars[-1].bar.close == Decimal("100.5")


@pytest.mark.regression
@pytest.mark.parametrize("delay, first_offset", [(5_000, 0), (5_001, 5_001)])
def test_close_availability_and_revisions_follow_receipt_time(delay, first_offset) -> None:
    # ADR-0018: the grace period applies only to the first confirmed version.
    supervisor, events = make()
    end = ts(1)
    supervisor.ingest(kline(ts(0), "99"), now=end - 1)
    supervisor.ingest(kline(ts(0), "100", confirmed=True), now=end + delay)
    supervisor.ingest(kline(ts(0), "101", confirmed=True), now=end + 7_000)
    supervisor.ingest(kline(ts(0), "102", confirmed=True), now=end + 6_000)

    assert [event.bar.ts_available for event in klines(events) if event.confirmed] == [
        end + first_offset, end + 7_000, end + 7_000,
    ]


def test_missing_minutes_without_transport_loss_become_no_trades() -> None:
    clock = Clock(ts(0) + 1_000)
    supervisor, events = make(clock)

    supervisor.ingest(kline(ts(0), "100", confirmed=True), now=clock())
    clock.set(ts(2) + 1_000)
    supervisor.ingest(kline(ts(2), "102", confirmed=True), now=clock())

    bars = klines(events)
    assert [event.bar.timestamp for event in bars] == [ts(0), ts(1), ts(2)]
    synthesized = bars[1].bar
    assert synthesized.status is BarStatus.NO_TRADES
    assert synthesized.close == Decimal("100")
    assert synthesized.open == synthesized.high == synthesized.low == Decimal("100")
    assert synthesized.volume == Decimal("0")
    assert bars[1].confirmed is True and bars[1].revision == 0


def test_transport_loss_marks_stale_and_reconnect_synthesizes_feed_gap() -> None:
    clock = Clock(ts(0) + 1_000)
    supervisor, events = make(clock)
    supervisor.ingest(kline(ts(0), "100", confirmed=True), now=clock())

    supervisor.transport_lost(now=clock.set(ts(0) + 10_000))
    clock.set(ts(2) + 10_000)
    supervisor.transport_restored(now=clock())

    statuses = [event.status for event in healths(events)]
    assert statuses == [
        FeedHealthStatus.LIVE,
        FeedHealthStatus.STALE,
        FeedHealthStatus.RECOVERING,
        FeedHealthStatus.LIVE,
    ]
    assert healths(events)[1].stale_since == ts(0) + 1_000
    gaps = [event for event in klines(events) if event.bar.status is BarStatus.FEED_GAP]
    assert [event.bar.timestamp for event in gaps] == [ts(1)]
    assert gaps[0].bar.open is None and gaps[0].bar.close is None
    assert supervisor.health().status is FeedHealthStatus.LIVE


def test_rest_backfill_fills_gap_and_is_idempotent() -> None:
    clock = Clock(ts(0) + 1_000)
    connector = StubConnector()
    supervisor, events = make(clock, connector)
    supervisor.ingest(kline(ts(0), "100", confirmed=True), now=clock())

    supervisor.transport_lost(now=clock.set(ts(0) + 10_000))
    backfill = [raw(ts(1), "105")]
    supervisor.transport_restored(now=clock.set(ts(2) + 10_000), backfill=backfill)

    bars = klines(events)
    assert [event.bar.timestamp for event in bars] == [ts(0), ts(1)]
    assert bars[-1].bar.close == Decimal("105")
    assert bars[-1].confirmed is True
    assert connector.calls == []

    supervisor.transport_lost(now=clock.set(ts(3) + 10_000))
    supervisor.transport_restored(now=clock.set(ts(3) + 20_000), backfill=backfill)
    # 同一份回填重复应用不产生第二条 ts(1) 事件；跨越断流区间起点的柱记 FEED_GAP。
    assert [event for event in klines(events) if event.bar.timestamp == ts(1)] == [bars[-1]]
    assert klines(events)[-1].bar.timestamp == ts(2)
    assert klines(events)[-1].bar.status is BarStatus.FEED_GAP

    supervisor.ingest(kline(ts(1), "106", confirmed=True), now=clock.advance(10))
    bars = klines(events)
    assert bars[-1].revision == 1
    assert bars[-1].bar.close == Decimal("106")


def test_tick_declares_stale_after_twelve_seconds_of_silence() -> None:
    clock = Clock(BASE + 1_000)
    supervisor, events = make(clock)
    supervisor.ingest(book(now=clock()), now=clock())

    supervisor.tick(now=clock.set(BASE + 12_999))
    assert supervisor.health().status is FeedHealthStatus.LIVE
    assert len(healths(events)) == 1

    supervisor.tick(now=clock.set(BASE + 13_000))
    stale = healths(events)[-1]
    assert stale.status is FeedHealthStatus.STALE
    assert stale.stale_since == BASE + 1_000
    before = len(healths(events))

    supervisor.tick(now=clock.set(BASE + 20_000))
    assert len(healths(events)) == before

    supervisor.ingest(book(now=clock()), now=clock())
    assert healths(events)[-1].status is FeedHealthStatus.LIVE
    assert healths(events)[-1].stale_since is None


def test_health_report_aggregates_worst_status_per_channel() -> None:
    clock = Clock(BASE)
    supervisor, _ = make(clock, subscribe=False)
    supervisor.subscribe(instrument_id="BTC-USDT-PERP", timeframe="1m")
    supervisor.subscribe(instrument_id="ETH-USDT-PERP", timeframe="1m")
    supervisor.ingest(book(now=clock.set(BASE + 5_000)), now=clock())

    supervisor.tick(now=clock.set(BASE + 12_000))
    report = supervisor.health()
    assert report.status is FeedHealthStatus.STALE
    assert report.feed_mode == "stub"
    assert [channel.instrument_id for channel in report.channels] == [
        "BTC-USDT-PERP",
        "ETH-USDT-PERP",
    ]
    assert [channel.timeframe for channel in report.channels] == ["1m", "1m"]
    assert [channel.status for channel in report.channels] == [
        FeedHealthStatus.LIVE,
        FeedHealthStatus.STALE,
    ]

    supervisor.unsubscribe(instrument_id="ETH-USDT-PERP", timeframe="1m")
    report = supervisor.health()
    assert report.status is FeedHealthStatus.LIVE
    assert len(report.channels) == 1


def test_unsubscribed_instrument_events_are_dropped() -> None:
    supervisor, events = make()
    supervisor.unsubscribe(instrument_id=INSTRUMENT, timeframe="1m")

    supervisor.ingest(kline(ts(0), "100"), now=BASE + 1_000)

    assert klines(events) == []


def test_subscribe_rejects_unknown_instrument_and_timeframe() -> None:
    supervisor, _ = make(subscribe=False)

    with pytest.raises(InstrumentNotFoundError):
        supervisor.subscribe(instrument_id="DOGE-USDT-PERP", timeframe="1m")
    with pytest.raises(InvalidRequestError):
        supervisor.subscribe(instrument_id=INSTRUMENT, timeframe="2m")


def derivative_tick(now: int = BASE, *, instrument_id: str = INSTRUMENT) -> DerivativeTickEvent:
    return DerivativeTickEvent(
        instrument_id=instrument_id,
        mark_price=Decimal("100.5"),
        funding_rate=Decimal("0.0001"),
        next_funding_ts=now + 8 * 3_600_000,
        open_interest=Decimal("12345.6"),
        ts_event=now,
        ts_ingested=now,
    )


def test_derivative_tick_is_emitted_without_touching_bar_series() -> None:
    clock = Clock(BASE + 1_000)
    supervisor, events = make(clock)

    supervisor.ingest(derivative_tick(clock()), now=clock())

    ticks = [event for event in events if isinstance(event, DerivativeTickEvent)]
    assert len(ticks) == 1
    assert ticks[0].mark_price == Decimal("100.5")
    assert supervisor.recent_bars(instrument_id=INSTRUMENT, timeframe="1m") == []


def test_derivative_tick_for_unsubscribed_instrument_is_dropped() -> None:
    clock = Clock(BASE + 1_000)
    supervisor, events = make(clock)

    supervisor.ingest(derivative_tick(clock(), instrument_id="ETH-USDT-PERP"), now=clock())

    assert not [event for event in events if isinstance(event, DerivativeTickEvent)]
