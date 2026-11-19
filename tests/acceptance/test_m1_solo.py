"""M1 单人验收：离线 fixture + 回放走通一条完整交易员场景。

阶段 A（fixture，无实时流）：建 Watchlist、冻结快照、读 K 线/指标/衍生品/
关键位、画线并挂两类预警。
阶段 B（同一 data_dir，replay）：重启后加载已持久化预警，用确定性 K 线触发
关键位触及与画线穿越，断流/恢复，扫描器产出一张异动卡片与 SCANNER_FOCUS。
阶段 C（无 replay 再重启）：按 snapshot_id 读回逐字节一致的快照与预警记录。

测试不依赖网络、真实模型或墙钟时间：``AppSettings.now_ms`` 固定为 NOW_MS，
回放脚本由 fixture 末根 K 线推导，扫描器手动触发，等待全部用轮询。
"""

from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from talaria.workbench_api.app import create_app
from talaria.workbench_api.settings import AppSettings

pytestmark = pytest.mark.medium

TOKEN = "m1-acceptance-token"
NOW_MS = 1_795_022_974_103
AUTH = {"Authorization": f"Bearer {TOKEN}"}
INSTRUMENT = "BTC-USDT-PERP"
TIMEFRAME = "1m"


class MessagePump:
    """后台线程收 WS 帧；已收帧保留在 ``seen`` 供事后断言。"""

    def __init__(self, websocket) -> None:  # noqa: ANN001 - TestClient 会话对象
        self.seen: list[dict] = []
        self._queue: queue.Queue[dict] = queue.Queue()
        self._thread = threading.Thread(target=self._run, args=(websocket,), daemon=True)
        self._thread.start()

    def _run(self, websocket) -> None:  # noqa: ANN001
        try:
            while True:
                message = websocket.receive_json()
                self.seen.append(message)
                self._queue.put(message)
        except Exception:
            return

    def next(self, timeout: float = 10.0) -> dict:
        return self._queue.get(timeout=timeout)

    def wait_for(self, predicate, timeout: float = 15.0) -> dict:  # noqa: ANN001
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in list(self.seen):
                if predicate(message):
                    return message
            time.sleep(0.05)
        raise AssertionError(
            "等待 WS 帧超时；bar 状态 "
            f"{[m['data']['bar']['status'] for m in self.seen if m.get('type') == 'bar']}"
        )


def _settings(
    data_dir: Path,
    dist_dir: Path,
    fixture_dir: Path,
    replay_events: Path | None = None,
) -> AppSettings:
    return AppSettings(
        data_dir=data_dir,
        dist_dir=dist_dir,
        fixture_dir=fixture_dir,
        replay_events=replay_events,
        replay_speed=4.0,
        token=TOKEN,
        now_ms=lambda: NOW_MS,
    )


def _wait_until(predicate, timeout: float = 25.0, interval: float = 0.1):  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("等待条件超时")


def _decimal(value: Decimal) -> str:
    return format(value, "f")


def _bar_event(
    *,
    instrument_id: str,
    timestamp: int,
    close: Decimal,
    high: Decimal,
    low: Decimal,
    volume: str = "100",
    turnover: str = "100000",
) -> dict:
    return {
        "offset_ms": 0,
        "type": "kline",
        "event": {
            "instrument_id": instrument_id,
            "timeframe": TIMEFRAME,
            "confirmed": True,
            "revision": 0,
            "bar": {
                "timestamp": timestamp,
                "open": _decimal(close),
                "high": _decimal(high),
                "low": _decimal(low),
                "close": _decimal(close),
                "volume": volume,
                "turnover": turnover,
                "status": "NORMAL",
                "ts_available": timestamp + 60_000,
                "precision": "SECOND_VERIFIED",
            },
        },
    }


def _write_replay(path: Path, *, level_price: Decimal, last_fixture_ts: int) -> dict:
    """写一段确定性回放：24 根平静柱 → 触及关键位 → 穿越画线 → 放量 →
    断流 → 跳过一分钟的恢复柱（制造 FEED_GAP）。"""

    start = last_fixture_ts + 60_000
    calm = level_price - Decimal("200")
    events: list[dict] = [{"offset_ms": 0, "type": "heartbeat"}]
    bars: list[dict] = []

    def append(event: dict, offset_ms: int) -> None:
        event["offset_ms"] = offset_ms
        events.append(event)

    offset = 8_000
    for index in range(24):
        bar = _bar_event(
            instrument_id=INSTRUMENT,
            timestamp=start + index * 60_000,
            close=calm,
            high=calm + Decimal("10"),
            low=calm - Decimal("10"),
        )
        bar["offset_ms"] = offset
        events.append(bar)
        bars.append(bar["event"]["bar"])
        offset += 40

    touch_offset = offset
    touch = _bar_event(
        instrument_id=INSTRUMENT,
        timestamp=start + 24 * 60_000,
        close=level_price,
        high=level_price + Decimal("5"),
        low=level_price - Decimal("5"),
    )
    touch["offset_ms"] = touch_offset
    events.append(touch)
    bars.append(touch["event"]["bar"])

    cross_offset = touch_offset + 40
    cross = _bar_event(
        instrument_id=INSTRUMENT,
        timestamp=start + 25 * 60_000,
        close=level_price + Decimal("110"),
        high=level_price + Decimal("120"),
        low=level_price + Decimal("60"),
    )
    cross["offset_ms"] = cross_offset
    events.append(cross)
    bars.append(cross["event"]["bar"])

    follow_offset = cross_offset + 40
    follow = _bar_event(
        instrument_id=INSTRUMENT,
        timestamp=start + 26 * 60_000,
        close=level_price + Decimal("150"),
        high=level_price + Decimal("160"),
        low=level_price + Decimal("100"),
    )
    follow["offset_ms"] = follow_offset
    events.append(follow)
    bars.append(follow["event"]["bar"])

    spike_offset = follow_offset + 40
    spike = _bar_event(
        instrument_id=INSTRUMENT,
        timestamp=start + 27 * 60_000,
        close=level_price + Decimal("250"),
        high=level_price + Decimal("300"),
        low=level_price + Decimal("200"),
        volume="999999",
        turnover="99999900",
    )
    spike["offset_ms"] = spike_offset
    events.append(spike)
    bars.append(spike["event"]["bar"])

    # 扫描器只评估最新一根柱；放量柱之后不再追加正常柱，手动扫描才稳定命中。
    disconnect_offset = spike_offset + 1_000
    append(
        {"offset_ms": disconnect_offset, "type": "disconnect", "reason": "验收模拟行情断流"},
        disconnect_offset,
    )
    reconnect_offset = disconnect_offset + 2_400
    append({"offset_ms": reconnect_offset, "type": "reconnect"}, reconnect_offset)

    recovery_offset = reconnect_offset + 200
    recovery_ts = start + 29 * 60_000  # 跳过 28 号槽位，触发一根 FEED_GAP
    recovery = _bar_event(
        instrument_id=INSTRUMENT,
        timestamp=recovery_ts,
        close=level_price + Decimal("270"),
        high=level_price + Decimal("280"),
        low=level_price + Decimal("220"),
    )
    recovery["offset_ms"] = recovery_offset
    events.append(recovery)
    bars.append(recovery["event"]["bar"])

    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    return {
        "start": start,
        "touch_ts": start + 24 * 60_000,
        "spike_ts": start + 27 * 60_000,
        "gap_ts": start + 28 * 60_000,
        "recovery_ts": recovery_ts,
    }


def test_m1_solo_acceptance_flow(tmp_path: Path, fixture_dir: Path) -> None:
    data_dir = tmp_path / "data"
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text(
        "<!doctype html><title>Talaria</title>", encoding="utf-8"
    )

    # ------------------------------------------------------------------ 阶段 A
    with TestClient(create_app(_settings(data_dir, dist_dir, fixture_dir))) as client:
        health = client.get("/api/health").json()
        assert health["feed_mode"] == "fixture"
        collaboration = client.get("/api/collaboration").json()
        assert collaboration["mode"] == "solo"

        # 写操作必须带会话 Bearer Token（§5.3 门禁）。
        assert (
            client.post("/api/watchlist/groups", json={"name": "非法写入"}).status_code
            == 401
        )

        group_resp = client.post(
            "/api/watchlist/groups", json={"name": "M1 验收"}, headers=AUTH
        )
        assert group_resp.status_code == 200
        group = next(
            item for item in group_resp.json()["groups"] if item["name"] == "M1 验收"
        )
        assert group["kind"] == "MANUAL"
        item_resp = client.post(
            "/api/watchlist/items",
            json={"group_id": group["group_id"], "instrument_id": INSTRUMENT},
            headers=AUTH,
        )
        assert item_resp.status_code == 200
        watchlist = item_resp.json()
        manual = next(g for g in watchlist["groups"] if g["group_id"] == group["group_id"])
        assert [entry["instrument_id"] for entry in manual["items"]] == [INSTRUMENT]
        # fixture-only 模式没有实时流，订阅配额为零是预期；阶段 B 才拉起 supervisor。
        assert watchlist["realtime_quota"] == 0

        snapshot_resp = client.post(
            "/api/snapshots",
            json={
                "instrument_id": INSTRUMENT,
                "timeframes": [TIMEFRAME, "5m"],
                "cutoff_ts": NOW_MS,
            },
            headers=AUTH,
        )
        assert snapshot_resp.status_code == 200
        meta_a = snapshot_resp.json()
        snapshot_id = meta_a["snapshot_id"]
        assert meta_a["instrument_id"] == INSTRUMENT
        assert meta_a["cutoff_ts"] == NOW_MS
        assert meta_a["bar_counts"]["1m"] == 905
        assert meta_a["bar_counts"]["5m"] == 400

        bars_resp = client.get(
            f"/api/snapshots/{snapshot_id}/bars",
            params={"timeframe": TIMEFRAME, "limit": 1000},
        )
        assert bars_resp.status_code == 200
        page_a = bars_resp.json()
        assert len(page_a["bars"]) == 905
        assert page_a["bars"][-1]["close"] == "68685.3"
        statuses = {bar["status"] for bar in page_a["bars"]}
        assert {"FEED_GAP", "NO_TRADES"} <= statuses
        gap = next(bar for bar in page_a["bars"] if bar["status"] == "FEED_GAP")
        assert gap["close"] is None

        indicators_resp = client.get(
            f"/api/snapshots/{snapshot_id}/indicators",
            params={"timeframe": TIMEFRAME, "names": "EMA,RSI", "limit": 5},
        )
        assert indicators_resp.status_code == 200
        indicator_series = {
            series["name"]: series for series in indicators_resp.json()["series"]
        }
        assert set(indicator_series) == {"EMA", "RSI"}
        ema_latest = indicator_series["EMA"]["summary"]["latest"]["value"]
        rsi_latest = indicator_series["RSI"]["summary"]["latest"]["value"]
        assert ema_latest is not None and isinstance(ema_latest, str)
        assert rsi_latest is not None and isinstance(rsi_latest, str)
        assert indicator_series["EMA"]["summary"]["valid_points"] > 0
        assert indicator_series["EMA"]["points"][-1]["values"]["value"] == ema_latest
        assert len(indicator_series["EMA"]["points"]) == 5

        derivatives_resp = client.get(
            f"/api/snapshots/{snapshot_id}/derivatives",
            params={"timeframe": "5m"},
        )
        assert derivatives_resp.status_code == 200
        derivative_summary = derivatives_resp.json()["summary"]
        assert derivative_summary["mark_price"] is not None
        assert derivative_summary["funding_rate"] is not None
        assert derivative_summary["open_interest"] is not None
        assert derivative_summary["next_funding_ts"] is not None
        assert isinstance(derivative_summary["mark_price"], str)

        levels_resp = client.get(
            f"/api/snapshots/{snapshot_id}/key-levels",
            params={"timeframe": TIMEFRAME, "limit": 200},
        )
        assert levels_resp.status_code == 200
        level_set_a = levels_resp.json()
        assert level_set_a["snapshot_id"] == snapshot_id
        assert level_set_a["timeframe"] == TIMEFRAME
        strengths = [level["strength"] for level in level_set_a["levels"]]
        assert len(level_set_a["levels"]) >= 20
        # 默认 strength_desc：强度单调不增（同强度再按时间与 id 稳定排序）。
        assert strengths == sorted(strengths, reverse=True)
        chosen = next(
            level
            for level in level_set_a["levels"]
            if level["price"] is not None and level["price_low"] is None
        )
        level_price = Decimal(chosen["price"])
        assert level_price > 0
        assert chosen["level_id"].startswith("kl_")
        assert chosen["category"] and chosen["kind"]

        filtered_resp = client.get(
            f"/api/snapshots/{snapshot_id}/key-levels",
            params={
                "timeframe": TIMEFRAME,
                "categories": "STRUCTURE",
                "min_strength": 0.9,
                "limit": 200,
            },
        )
        filtered_levels = filtered_resp.json()["levels"]
        assert filtered_levels
        assert all(level["category"] == "STRUCTURE" for level in filtered_levels)
        assert all(level["strength"] >= 0.9 for level in filtered_levels)
        filtered_ids = {level["level_id"] for level in filtered_levels}
        assert filtered_ids <= {level["level_id"] for level in level_set_a["levels"]}
        assert len(filtered_ids) < len(level_set_a["levels"])

        drawing_price = level_price + Decimal("100")
        drawing_resp = client.post(
            "/api/drawings",
            json={
                "instrument_id": INSTRUMENT,
                "kind": "HORIZONTAL_LINE",
                "visible_timeframes": [TIMEFRAME, "5m"],
                "points": [{"ts": NOW_MS, "price": _decimal(drawing_price)}],
                "text": "验收阻力线",
            },
            headers=AUTH,
        )
        assert drawing_resp.status_code == 201
        drawing = drawing_resp.json()
        assert drawing["kind"] == "HORIZONTAL_LINE"
        assert drawing["points"][0]["price"] == _decimal(drawing_price)

        level_alert_resp = client.post(
            "/api/alerts",
            json={
                "instrument_id": INSTRUMENT,
                "timeframe": TIMEFRAME,
                "condition": {
                    "kind": "LEVEL_EVENT",
                    "level_id": chosen["level_id"],
                    "level_event": "TOUCH",
                    "level_kind": chosen["kind"],
                },
                "note": "关键位触及验收",
                "repeat": False,
                "cooldown_ms": 0,
            },
            headers=AUTH,
        )
        assert level_alert_resp.status_code == 201
        level_alert = level_alert_resp.json()
        assert level_alert["status"] == "ARMED"
        assert level_alert["condition"]["level_id"] == chosen["level_id"]

        overlay_alert_resp = client.post(
            "/api/alerts",
            json={
                "instrument_id": INSTRUMENT,
                "timeframe": TIMEFRAME,
                "condition": {
                    "kind": "OVERLAY_CROSS",
                    "drawing_id": drawing["drawing_id"],
                    "overlay_mode": "TOUCH",
                    "direction": "ABOVE",
                },
                "note": "画线穿越验收",
                "repeat": False,
                "cooldown_ms": 0,
            },
            headers=AUTH,
        )
        assert overlay_alert_resp.status_code == 201
        overlay_alert = overlay_alert_resp.json()
        assert overlay_alert["status"] == "ARMED"

        armed = client.get("/api/alerts").json()["alerts"]
        assert {alert["alert_id"] for alert in armed} >= {
            level_alert["alert_id"],
            overlay_alert["alert_id"],
        }
        assert client.get("/api/alerts/triggers").json()["triggers"] == []

        parquet_a = data_dir / "state" / "hot_snapshots" / f"{snapshot_id}.parquet"
        parquet_digest_a = hashlib.sha256(parquet_a.read_bytes()).hexdigest()

    # ------------------------------------------------------------------ 阶段 B
    replay_path = tmp_path / "m1-replay.jsonl"
    replay = _write_replay(
        replay_path,
        level_price=level_price,
        last_fixture_ts=int(page_a["bars"][-1]["timestamp"]),
    )

    with TestClient(
        create_app(_settings(data_dir, dist_dir, fixture_dir, replay_path))
    ) as client:
        assert client.get("/api/health").json()["feed_mode"] == "replay"
        loaded = client.get("/api/alerts").json()["alerts"]
        assert {alert["alert_id"] for alert in loaded} >= {
            level_alert["alert_id"],
            overlay_alert["alert_id"],
        }

        # 重启后先冻结一次快照，关键位缓存才有内容；同时验证同一 level_id 仍在。
        rebuild_resp = client.post(
            "/api/snapshots",
            json={
                "instrument_id": INSTRUMENT,
                "timeframes": [TIMEFRAME],
                "cutoff_ts": NOW_MS,
            },
            headers=AUTH,
        )
        assert rebuild_resp.status_code == 200
        rebuilt_snapshot_id = rebuild_resp.json()["snapshot_id"]
        assert rebuilt_snapshot_id != snapshot_id
        rebuilt_levels = client.get(
            f"/api/snapshots/{rebuilt_snapshot_id}/key-levels",
            params={"timeframe": TIMEFRAME, "limit": 200},
        ).json()["levels"]
        assert chosen["level_id"] in {level["level_id"] for level in rebuilt_levels}

        with client.websocket_connect("/ws/desk") as websocket:
            pump = MessagePump(websocket)
            hello = pump.next()
            assert hello["type"] == "hello"
            websocket.send_json(
                {"op": "subscribe", "instrument_id": INSTRUMENT, "timeframe": TIMEFRAME}
            )

            triggers = _wait_until(
                lambda: (
                    lambda items: items
                    if {item["alert_id"] for item in items}
                    >= {level_alert["alert_id"], overlay_alert["alert_id"]}
                    else None
                )(
                    client.get(
                        "/api/alerts/triggers",
                        params={"instrument_id": INSTRUMENT, "limit": 50},
                    ).json()["triggers"]
                ),
                timeout=30.0,
            )
            level_trigger = next(
                item
                for item in triggers
                if item["alert_id"] == level_alert["alert_id"]
            )
            overlay_trigger = next(
                item
                for item in triggers
                if item["alert_id"] == overlay_alert["alert_id"]
            )
            assert level_trigger["condition_kind"] == "LEVEL_EVENT"
            assert level_trigger["observed"] is not None
            assert overlay_trigger["condition_kind"] == "OVERLAY_CROSS"
            assert level_trigger["snapshot_id"] and overlay_trigger["snapshot_id"]
            assert client.get(
                f"/api/snapshots/{level_trigger['snapshot_id']}"
            ).status_code == 200
            assert client.get(
                f"/api/snapshots/{overlay_trigger['snapshot_id']}"
            ).status_code == 200

            # 等放量柱到达后再手动扫描，避免依赖 3 秒 ticker 的时序。
            pump.wait_for(
                lambda message: message.get("type") == "bar"
                and message["data"]["bar"]["timestamp"] >= replay["spike_ts"],
                timeout=15.0,
            )
            scan_resp = client.post(
                "/api/scanner/scan",
                json={"instrument_ids": [INSTRUMENT]},
                headers=AUTH,
            )
            assert scan_resp.status_code == 200
            scan_triggers = scan_resp.json()["triggers"]
            liquidity = [
                item for item in scan_triggers if item["kind"] == "LIQUIDITY_ANOMALY"
            ]
            if not liquidity:
                liquidity = _wait_until(
                    lambda: (
                        lambda items: items or None
                    )(
                        [
                            item
                            for item in client.get(
                                "/api/scanner/triggers",
                                params={"instrument_id": INSTRUMENT, "limit": 50},
                            ).json()["triggers"]
                            if item["kind"] == "LIQUIDITY_ANOMALY"
                        ]
                    ),
                    timeout=5.0,
                )
            assert liquidity[0]["trigger_id"]
            assert liquidity[0]["snapshot_id"]
            assert client.get(
                f"/api/snapshots/{liquidity[0]['snapshot_id']}"
            ).status_code == 200

            focus = client.get("/api/scanner/focus").json()
            assert focus["group_id"] == "scanner-focus"
            assert INSTRUMENT in focus["instrument_ids"]
            watchlist_b = client.get("/api/watchlist").json()
            focus_group = next(
                group
                for group in watchlist_b["groups"]
                if group["kind"] == "SCANNER_FOCUS"
            )
            assert focus_group["is_system"] is True
            assert INSTRUMENT in [
                entry["instrument_id"] for entry in focus_group["items"]
            ]

            # 断流：supervisor 直接把已订阅通道置 STALE。
            assert _wait_until(
                lambda: client.get("/api/feed/health").json()["status"] == "STALE",
                timeout=15.0,
            )
            assert _wait_until(
                lambda: client.get("/api/feed/health").json()["status"] == "LIVE",
                timeout=15.0,
            )

            gap_frame = pump.wait_for(
                lambda message: message.get("type") == "bar"
                and message["data"]["bar"]["status"] == "FEED_GAP",
                timeout=10.0,
            )
            assert gap_frame["data"]["bar"]["close"] is None
            assert gap_frame["data"]["bar"]["timestamp"] == replay["gap_ts"]

            health_statuses = {
                message["data"]["status"]
                for message in pump.seen
                if message.get("type") == "health"
            }
            assert {"STALE", "RECOVERING", "LIVE"} <= health_statuses
            alert_frames = [
                message for message in pump.seen if message.get("type") == "alert"
            ]
            assert {frame["data"]["alert_id"] for frame in alert_frames} >= {
                level_alert["alert_id"],
                overlay_alert["alert_id"],
            }

    # ------------------------------------------------------------------ 阶段 C
    with TestClient(create_app(_settings(data_dir, dist_dir, fixture_dir))) as client:
        watchlist_c = client.get("/api/watchlist").json()
        names = {group["name"] for group in watchlist_c["groups"]}
        assert "M1 验收" in names
        assert "扫描焦点" in names

        meta_c = client.get(f"/api/snapshots/{snapshot_id}").json()
        assert meta_c == meta_a
        page_c = client.get(
            f"/api/snapshots/{snapshot_id}/bars",
            params={"timeframe": TIMEFRAME, "limit": 1000},
        ).json()
        assert page_c == page_a
        parquet_c = data_dir / "state" / "hot_snapshots" / f"{snapshot_id}.parquet"
        assert hashlib.sha256(parquet_c.read_bytes()).hexdigest() == parquet_digest_a

        indicators_c = client.get(
            f"/api/snapshots/{snapshot_id}/indicators",
            params={"timeframe": TIMEFRAME, "names": "EMA,RSI", "limit": 5},
        ).json()
        series_c = {series["name"]: series for series in indicators_c["series"]}
        assert series_c["EMA"]["summary"]["latest"]["value"] == ema_latest
        assert series_c["RSI"]["summary"]["latest"]["value"] == rsi_latest

        levels_c = client.get(
            f"/api/snapshots/{snapshot_id}/key-levels",
            params={"timeframe": TIMEFRAME, "limit": 200},
        ).json()
        assert {level["level_id"] for level in levels_c["levels"]} == {
            level["level_id"] for level in level_set_a["levels"]
        }

        triggers_c = client.get(
            "/api/alerts/triggers", params={"instrument_id": INSTRUMENT, "limit": 50}
        ).json()["triggers"]
        assert {item["alert_id"] for item in triggers_c} >= {
            level_alert["alert_id"],
            overlay_alert["alert_id"],
        }
        persisted = {
            item["alert_id"]: item for item in triggers_c
        }
        assert client.get(
            f"/api/snapshots/{persisted[level_alert['alert_id']]['snapshot_id']}"
        ).status_code == 200
        assert client.get(f"/api/alerts/{level_alert['alert_id']}").status_code == 200
        assert (
            client.delete(
                f"/api/alerts/{level_alert['alert_id']}",
            ).status_code
            == 401
        )


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_healthy(port: int, process, timeout: float = 20.0) -> dict:  # noqa: ANN001
    import subprocess
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/api/health"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            raise AssertionError(
                f"守护进程提前退出（{process.returncode}）\nstdout={stdout}\nstderr={stderr}"
            )
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:  # noqa: S310
                if response.status == 200:
                    return json.loads(response.read())
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise AssertionError(f"健康检查超时：{url}（{last_error}）")


def test_m1_solo_cli_loopback_and_token(tmp_path: Path, fixture_dir: Path, repo_root: Path) -> None:
    """真实 ``talaria up --solo`` 子进程：只能绑回环，且写操作必须带会话 Token。"""

    import subprocess
    import sys

    import httpx

    # 非回环绑定在启动前即被拒绝，不会打开端口。
    rejected = subprocess.run(
        [
            sys.executable,
            "-m",
            "talaria",
            "up",
            "--solo",
            "--host",
            "0.0.0.0",
            "--port",
            str(_free_port()),
            "--data-dir",
            str(tmp_path / "rejected-data"),
            "--fixture-dir",
            str(fixture_dir),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert rejected.returncode != 0
    assert "回环" in rejected.stdout + rejected.stderr

    port = _free_port()
    command = [
        sys.executable,
        "-m",
        "talaria",
        "up",
        "--solo",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--data-dir",
        str(tmp_path / "data"),
        "--fixture-dir",
        str(fixture_dir),
    ]
    process = subprocess.Popen(
        command,
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        health = _wait_healthy(port, process)
        assert health["ok"] is True
        assert health["feed_mode"] == "fixture"

        base = f"http://127.0.0.1:{port}"
        with httpx.Client(trust_env=False, timeout=5.0) as client:
            desk = client.get(f"{base}/desk")
            assert desk.status_code == 200
            token = client.cookies.get("talaria_token")
            assert token

            # 读端点放行；写端点缺 Bearer 必须 401。
            assert client.get(f"{base}/api/watchlist").status_code == 200
            denied = client.post(f"{base}/api/watchlist/groups", json={"name": "无令牌"})
            assert denied.status_code == 401
            allowed = client.post(
                f"{base}/api/watchlist/groups",
                json={"name": "回环验收"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert allowed.status_code == 200
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
