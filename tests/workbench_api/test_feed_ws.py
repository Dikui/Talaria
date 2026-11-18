"""``/ws/desk`` 快通道与 ``/api/feed/health`` 的端到端测试。

回放文件放在 ``tests/fixtures/realtime``：偏移、断流与重连都由脚本决定，
测试只等消息、不依赖真实网络，也不用固定 sleep 猜测时序。
"""

from __future__ import annotations

import json
import queue
import re
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from talaria.foundation import FeedHealthEvent, FeedHealthStatus
from talaria.workbench_api.app import _wanted, create_app
from talaria.workbench_api.settings import AppSettings

BASE = 1_763_596_800_000
MINUTE = 60_000
INSTRUMENT = "BTC-USDT-PERP"
AUTH = {"Authorization": "Bearer test-token"}


class MessagePump:
    """后台线程收帧 + 有界队列读取，避免测试在消息缺失时永久阻塞。"""

    def __init__(self, websocket) -> None:  # noqa: ANN001 - TestClient 的会话对象
        self._queue: queue.Queue[dict] = queue.Queue()
        self._thread = threading.Thread(target=self._run, args=(websocket,), daemon=True)
        self._thread.start()

    def _run(self, websocket) -> None:  # noqa: ANN001
        try:
            while True:
                self._queue.put(websocket.receive_json())
        except Exception:
            return

    def next(self, timeout: float = 10.0) -> dict:
        return self._queue.get(timeout=timeout)

    def until(self, predicate, limit: int = 40, timeout: float = 10.0) -> list[dict]:
        seen: list[dict] = []
        for _ in range(limit):
            message = self.next(timeout=timeout)
            seen.append(message)
            if predicate(seen):
                return seen
        return seen


def make_settings(tmp_path: Path, *, replay: str | None = None) -> AppSettings:
    (tmp_path / "dist").mkdir(exist_ok=True)
    return AppSettings(
        data_dir=tmp_path / "data",
        dist_dir=tmp_path / "dist",
        fixture_dir=Path(__file__).resolve().parents[1] / "fixtures" / "bybit",
        replay_events=Path(__file__).resolve().parents[1] / "fixtures" / "realtime" / replay
        if replay
        else None,
        token="test-token",
        now_ms=lambda: BASE + 10 * MINUTE,
    )


def bars(messages: list[dict]) -> list[dict]:
    return [item["data"] for item in messages if item["type"] == "bar"]


def healths(messages: list[dict]) -> list[dict]:
    return [item["data"] for item in messages if item["type"] == "health"]


def test_fixture_mode_reports_feed_unavailable(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.get("/api/feed/health")
        assert response.status_code == 503
        assert response.json()["code"] == "FEED_UNAVAILABLE"
        assert "离线样本" in response.json()["message"]

        with client.websocket_connect("/ws/desk") as websocket:
            hello = websocket.receive_json()
            assert hello["type"] == "hello"
            assert hello["feed_mode"] == "fixture"
            assert hello["live"] is False
            websocket.send_json(
                {"op": "subscribe", "instrument_id": INSTRUMENT, "timeframe": "1m"}
            )
            websocket.send_json({"op": "nope"})


def test_replay_stream_publishes_revisions_and_health(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, replay="in_progress.jsonl"))
    with TestClient(app) as client:
        with client.websocket_connect("/ws/desk") as websocket:
            pump = MessagePump(websocket)
            hello = pump.next()
            assert hello["type"] == "hello"
            assert hello["feed_mode"] == "replay"
            assert hello["live"] is True

            websocket.send_json(
                {"op": "subscribe", "instrument_id": INSTRUMENT, "timeframe": "1m"}
            )
            messages = pump.until(lambda seen: len(bars(seen)) >= 3)
            report = client.get("/api/feed/health", headers=AUTH).json()
            assert report["feed_mode"] == "replay"
            assert report["status"] == "LIVE"
            assert [channel["instrument_id"] for channel in report["channels"]] == [
                INSTRUMENT
            ]
            assert [channel["timeframe"] for channel in report["channels"]] == ["1m"]

        revisions = [event["revision"] for event in bars(messages)]
        assert revisions == [0, 1, 2]
        assert [event["confirmed"] for event in bars(messages)] == [False, False, True]
        assert {event["bar"]["timestamp"] for event in bars(messages)} == {BASE}
        assert healths(messages)[0]["status"] == "LIVE"


def test_missing_minutes_without_outage_become_no_trades(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, replay="no_trades.jsonl"))
    with TestClient(app) as client:
        with client.websocket_connect("/ws/desk") as websocket:
            pump = MessagePump(websocket)
            pump.next()
            websocket.send_json(
                {"op": "subscribe", "instrument_id": INSTRUMENT, "timeframe": "1m"}
            )
            messages = pump.until(lambda seen: len(bars(seen)) >= 4)

        synthesized = bars(messages)[1:3]
        assert [event["bar"]["timestamp"] for event in synthesized] == [
            BASE + MINUTE,
            BASE + 2 * MINUTE,
        ]
        assert [event["bar"]["status"] for event in synthesized] == [
            "NO_TRADES",
            "NO_TRADES",
        ]
        assert synthesized[0]["bar"]["volume"] == "0"
        assert synthesized[0]["bar"]["close"] == "100.0"


def test_outage_marks_stale_then_recovers_with_feed_gap(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, replay="outage_recovery.jsonl"))
    with TestClient(app) as client:
        with client.websocket_connect("/ws/desk") as websocket:
            pump = MessagePump(websocket)
            pump.next()
            websocket.send_json(
                {"op": "subscribe", "instrument_id": INSTRUMENT, "timeframe": "1m"}
            )
            messages = pump.until(
                lambda seen: len(bars(seen)) >= 3 and len(healths(seen)) >= 4
            )
            report = client.get("/api/feed/health", headers=AUTH).json()
            assert report["status"] == "LIVE"
            assert report["channels"][0]["reconnect_count"] == 1

        statuses = [event["status"] for event in healths(messages)]
        assert statuses[:4] == ["LIVE", "STALE", "RECOVERING", "LIVE"]
        stale = healths(messages)[1]
        assert stale["stale_since"] is not None

        gap = bars(messages)[1]
        assert gap["bar"]["status"] == "FEED_GAP"
        assert gap["bar"]["timestamp"] == BASE + MINUTE
        assert gap["bar"]["close"] is None
        assert bars(messages)[2]["bar"]["timestamp"] == BASE + 2 * MINUTE


def test_unsubscribe_releases_channel(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, replay="in_progress.jsonl"))
    with TestClient(app) as client:
        with client.websocket_connect("/ws/desk") as websocket:
            pump = MessagePump(websocket)
            pump.next()
            websocket.send_json(
                {"op": "subscribe", "instrument_id": INSTRUMENT, "timeframe": "1m"}
            )
            pump.until(lambda seen: len(healths(seen)) == 1)
            assert client.get("/api/feed/health", headers=AUTH).json()["channels"]

            websocket.send_json(
                {"op": "unsubscribe", "instrument_id": INSTRUMENT, "timeframe": "1m"}
            )
            assert client.get("/api/feed/health", headers=AUTH).json()["channels"] == []


def test_unknown_subscription_returns_error_frame(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, replay="in_progress.jsonl"))
    with TestClient(app) as client:
        with client.websocket_connect("/ws/desk") as websocket:
            pump = MessagePump(websocket)
            pump.next()
            websocket.send_json(
                {"op": "subscribe", "instrument_id": "UNKNOWN-PERP", "timeframe": "1m"}
            )
            error = pump.until(lambda seen: any(item["type"] == "error" for item in seen))[-1]
            assert error["type"] == "error"
            assert error["code"] == "INSTRUMENT_NOT_FOUND"


def test_replay_file_lines_are_valid_jsonl() -> None:
    realtime = Path(__file__).resolve().parents[1] / "fixtures" / "realtime"
    for path in sorted(realtime.glob("*.jsonl")):
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines, path.name
        offsets = []
        for line in lines:
            payload = json.loads(line)
            assert re.fullmatch(r"kline|book|heartbeat|disconnect|reconnect", payload["type"])
            offsets.append(payload["offset_ms"])
        assert offsets == sorted(offsets), path.name


@pytest.mark.parametrize("speed", [0, -1, -0.5])
def test_replay_speed_must_be_positive(tmp_path: Path, speed: float) -> None:
    with pytest.raises(ValueError, match="replay_speed"):
        AppSettings(
            data_dir=tmp_path / "data",
            dist_dir=tmp_path / "dist",
            replay_events=tmp_path / "missing.jsonl",
            replay_speed=speed,
        )


def test_subscription_filter_matches_timeframe_for_health() -> None:
    health = FeedHealthEvent(
        instrument_id=INSTRUMENT,
        timeframe="5m",
        status=FeedHealthStatus.LIVE,
        ts=BASE,
        reconnect_count=0,
    )
    assert _wanted(health, {(INSTRUMENT, "1m")}) is False
    assert _wanted(health, {(INSTRUMENT, "5m")}) is True
    assert _wanted(health, {(INSTRUMENT, "1m"), (INSTRUMENT, "5m")}) is True
