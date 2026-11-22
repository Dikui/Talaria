from __future__ import annotations

from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from talaria.foundation.bars import CompactBar
from talaria.foundation.connector import RawBar
from talaria.foundation.feed import KlineEvent
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.packs.testing import FixtureConnector
from talaria.perception.cold_store import ColdBatch, ColdReader, ColdStore
from talaria.workbench_api.app import create_app
from talaria.workbench_api.service_config import FeatureToggles
from talaria.workbench_api.settings import AppSettings

pytestmark = [pytest.mark.medium, pytest.mark.spec]

INSTRUMENT = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
TIMEFRAME = "1m"
MINUTE = 60_000
START = 1_795_045_500_000


class Market:
    def __init__(self, tmp_path: Path, fixture_dir: Path) -> None:
        self.now = START
        # Use an idle replay in place of a live exchange connection.
        replay = tmp_path / "events.jsonl"
        replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
        self.settings = AppSettings(
            data_dir=tmp_path / "data", dist_dir=tmp_path / "dist", fixture_dir=fixture_dir,
            replay_events=replay, now_ms=lambda: self.now,
            features=FeatureToggles(alerts=False, scanner=False),
        )
        self.app = create_app(self.settings)

    def archive(self, *bars: RawBar) -> None:
        with closing(ColdStore(self.settings.data_dir)) as store:
            store.publish(ColdBatch(INSTRUMENT, TIMEFRAME, bars))

    def ingest(self, client: TestClient, *events: KlineEvent) -> None:
        async def deliver() -> None:
            supervisor = self.app.state.runtime.supervisor
            supervisor.subscribe(instrument_id=INSTRUMENT.instrument_id, timeframe=TIMEFRAME)
            for event in events:
                supervisor.ingest(event)

        client.portal.call(deliver)


def _raw(timestamp: int, close: str, *, revision: int = 0, available: int | None = None) -> RawBar:
    value = Decimal(close)
    ts_available = timestamp + MINUTE if available is None else available
    return RawBar(
        symbol=INSTRUMENT.symbol, timeframe=TIMEFRAME, ts_event=timestamp,
        open=value, high=value, low=value, close=value, volume=Decimal("1"), turnover=value,
        ts_available=ts_available, ts_ingested=ts_available, ts_committed=ts_available,
        revision=revision,
    )


def _event(timestamp: int, close: str, *, confirmed: bool) -> KlineEvent:
    bar: CompactBar = _raw(timestamp, close).to_compact()
    return KlineEvent(
        instrument_id=INSTRUMENT.instrument_id, timeframe=TIMEFRAME,
        bar=bar, confirmed=confirmed, revision=0,
    )


def _get(client: TestClient, start: int, end: int, **options) -> list[dict]:
    response = client.get("/api/bars", params={
        "instrument": INSTRUMENT.instrument_id, "timeframe": TIMEFRAME,
        "start": start, "end": end, **options,
    })
    assert response.status_code == 200, response.text
    return response.json()["bars"]


def _disk_full(*args, **kwargs):
    raise OSError("test cold write failure")


def test_bars_span_cold_and_unpublished_hot_ranges(tmp_path, fixture_dir, monkeypatch):
    # data-access/03: hold the last confirmed bar before persistence at the cold writer seam.
    market = Market(tmp_path, fixture_dir)
    tail = START + MINUTE
    market.archive(_raw(START, "100.5"))
    market.now = tail + MINUTE

    monkeypatch.setattr(ColdStore, "publish", _disk_full)
    with TestClient(market.app) as client:
        market.ingest(
            client, _event(START, "100.5", confirmed=True), _event(tail, "101.2", confirmed=True),
        )
        bars = _get(client, START, tail)
        assert [bar["ts_event"] for bar in bars] == [START, tail]
        assert [bar["confirmed"] for bar in bars] == [True, True]
        assert [bar["revision"] for bar in bars] == [0, 0]
        assert all(isinstance(bar["close"], str) for bar in bars)
        assert [Decimal(bar["close"]) for bar in bars] == [Decimal("100.5"), Decimal("101.2")]
        assert _get(client, tail, tail) == bars[1:]


def test_as_of_selects_confirmed_revision_after_visibility_cutoff(tmp_path, fixture_dir):
    # data-access/03: Bybit's preview uses the scheduled close timestamp on the wire.
    market = Market(tmp_path, fixture_dir)
    end = START + MINUTE
    with TestClient(market.app) as client:
        market.now = START + 1_000
        market.ingest(client, _event(START, "98", confirmed=False))
        market.now = START + 2_000
        market.ingest(client, _event(START, "99", confirmed=False))
        market.now = end + 6_000
        assert _get(client, START, START) == []
        assert _get(client, START, START, include_open=False) == []
        assert _get(client, START, START, include_open=True)[0]["confirmed"] is False

        market.ingest(client, _event(START, "100", confirmed=True))
        market.now = end + 7_000
        market.ingest(client, _event(START, "101", confirmed=True))
        assert _get(client, START, START, as_of=end + 5_999) == []
        preview = _get(client, START, START, as_of=START + 1_000, include_open=True)[0]
        assert (preview["close"], preview["confirmed"], preview["revision"]) == ("98", False, 0)
        assert _get(client, START, START, as_of=end, include_open=True)[0]["close"] == "99"
        first = _get(client, START, START, as_of=end + 6_000)[0]
        assert Decimal(first["close"]) == 100
        assert (first["confirmed"], first["revision"]) == (True, 2)
        latest = _get(client, START, START)[0]
        assert Decimal(latest["close"]) == 101
        assert (latest["confirmed"], latest["revision"]) == (True, 3)

    cold = ColdReader(market.settings.data_dir).read_bars(INSTRUMENT, TIMEFRAME)
    assert [(bar.close, bar.revision) for bar in cold] == [(Decimal("101"), 3)]


def test_restart_correction_continues_archived_revision(tmp_path, fixture_dir, monkeypatch):
    # data-access/03: the durable revision stays stable once the pending correction lands.
    market = Market(tmp_path, fixture_dir)
    end = START + MINUTE
    market.archive(_raw(START, "100", revision=3))
    market.now = end + 3_000
    with monkeypatch.context() as patch:
        patch.setattr(ColdStore, "publish", _disk_full)
        with TestClient(market.app) as client:
            market.ingest(client, _event(START, "101", confirmed=True))
            before = _get(client, START, START, as_of=end)[0]
            latest = _get(client, START, START)[0]
            assert (Decimal(before["close"]), before["revision"]) == (Decimal("100"), 3)
            assert (Decimal(latest["close"]), latest["revision"]) == (Decimal("101"), 4)
            assert latest["ts_available"] == market.now

    with TestClient(market.app) as client:
        market.ingest(client, _event(START, "101", confirmed=True))

    cold = ColdReader(market.settings.data_dir)
    first = cold.read_bars(INSTRUMENT, TIMEFRAME, as_of=end)[0]
    latest = cold.read_bars(INSTRUMENT, TIMEFRAME)[0]
    assert (first.close, first.revision) == (Decimal("100"), 3)
    assert (latest.close, latest.revision, latest.ts_available) == (Decimal("101"), 4, market.now)


def test_exchange_close_replaces_a_synthetic_bar_at_the_grace_cutoff(tmp_path, fixture_dir, monkeypatch):
    # data-access/02: a synthetic zero-volume bar is only a hot fallback.
    market = Market(tmp_path, fixture_dir)
    synthetic_ts = START + MINUTE
    close_ts = synthetic_ts + MINUTE
    monkeypatch.setattr(ColdStore, "publish", _disk_full)
    with TestClient(market.app) as client:
        market.now = synthetic_ts
        market.ingest(client, _event(START, "100", confirmed=True))
        market.now = close_ts + 1_000
        market.ingest(client, _event(close_ts, "102", confirmed=False))
        synthetic = _get(client, synthetic_ts, synthetic_ts, as_of=close_ts)[0]
        assert (synthetic["close"], synthetic["volume"]) == ("100", "0")
        market.ingest(client, _event(synthetic_ts, "101", confirmed=True))
        actual = _get(client, synthetic_ts, synthetic_ts, as_of=close_ts)[0]
        assert (actual["close"], actual["ts_available"], actual["revision"]) == ("101", close_ts, 1)


def test_range_backfills_missing_history_without_confirming_a_live_preview(
    tmp_path, fixture_dir, monkeypatch,
):
    # data-access/02, ADR-0018: substitute fixture REST while leaving the writer active.
    market = Market(tmp_path, fixture_dir)
    tail = START + MINUTE
    market.now = tail + MINUTE + 10_000

    def fetch_bars(connector, *, symbol, timeframe, end_ts, limit):
        candidates = (_raw(START, "100"), _raw(tail, "102"))
        return [bar for bar in candidates if bar.ts_event <= end_ts][-limit:]

    monkeypatch.setattr(FixtureConnector, "fetch_bars", fetch_bars)
    with TestClient(market.app) as client:
        market.ingest(client, _event(tail, "101", confirmed=False))
        bars = _get(client, START, tail)
        assert [(bar["ts_event"], Decimal(bar["close"])) for bar in bars] == [(START, Decimal("100"))]
        assert _get(client, START, tail, as_of=tail - 1) == []
        with_preview = _get(client, START, tail, include_open=True)
        assert [(bar["ts_event"], bar["confirmed"]) for bar in with_preview] == [
            (START, True), (tail, False),
        ]

    offline = ColdReader(market.settings.data_dir).read_bars(INSTRUMENT, TIMEFRAME)
    assert [(bar.ts_event, bar.close) for bar in offline] == [(START, Decimal("100"))]
