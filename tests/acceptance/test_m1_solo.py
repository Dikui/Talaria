"""M1 单人验收：离线 fixture + 回放走通一条完整交易员场景。

阶段 A（fixture，无实时流）：建 Watchlist、冻结快照、读 K 线/指标/衍生品/
关键位、画线并挂两类预警。
阶段 B（同一 data_dir，replay）：重启后加载已持久化预警，用确定性 K 线触发
关键位触及与画线穿越，断流/恢复，扫描器产出一张异动卡片与 SCANNER_FOCUS。
阶段 C（无 replay 再重启）：按 snapshot_id 读回逐字节一致的快照与预警记录。

阶段 A 的时钟固定在快照截止时刻，回放从挂好预警后的下一分钟开始。
阶段 B、C 的业务时钟固定在回放末柱收盘时刻。阶段 B 按 WS 帧分段推进回放。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import queue
import threading
import time
from decimal import Decimal
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from talaria.replay import ReplayFeed
from talaria.workbench_api.app import create_app
from talaria.workbench_api.settings import AppSettings

pytestmark = pytest.mark.medium

TOKEN = "m1-acceptance-token"
SNAPSHOT_CUTOFF_MS = 1_795_022_974_103
AUTH = {"Authorization": f"Bearer {TOKEN}"}
INSTRUMENT = "BTC-USDT-PERP"
TIMEFRAME = "1m"
BARS_OFFSET_MS = 1000
DISCONNECT_OFFSET_MS = 2000
RECOVERY_OFFSET_MS = 3000


class ReplayClock:
    """只在 TestClient 的事件循环里推进 ReplayFeed 的单调时钟。"""

    def __init__(self) -> None:
        self.seconds = 0.0
        self._advanced = asyncio.Condition()

    def monotonic(self) -> float:
        return self.seconds

    async def sleep(self, delay: float) -> None:
        target = self.seconds + delay
        async with self._advanced:
            await self._advanced.wait_for(lambda: self.seconds >= target)

    async def advance_to(self, offset_ms: int) -> None:
        async with self._advanced:
            self.seconds = offset_ms / 1000
            self._advanced.notify_all()


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
                self._queue.put(message)
        except Exception:
            return

    def next(self, timeout: float = 10.0) -> dict:
        message = self._queue.get(timeout=timeout)
        self.seen.append(message)
        return message

    def wait_for(self, predicate, timeout: float = 15.0) -> dict:  # noqa: ANN001
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                message = self.next(timeout=remaining)
            except queue.Empty:
                break
            if predicate(message):
                return message
        raise AssertionError(
            "等待 WS 帧超时；bar 状态 "
            f"{[m['data']['bar']['status'] for m in self.seen if m.get('type') == 'bar']}"
        )


def _settings(
    data_dir: Path,
    dist_dir: Path,
    fixture_dir: Path,
    replay_events: Path | None = None,
    *,
    now_ms: int,
) -> AppSettings:
    return AppSettings(
        data_dir=data_dir,
        dist_dir=dist_dir,
        fixture_dir=fixture_dir,
        replay_events=replay_events,
        token=TOKEN,
        now_ms=lambda: now_ms,
    )


def _decimal(value: Decimal) -> str:
    return format(value, "f")


def _bar_event(
    *,
    instrument_id: str,
    timestamp: int,
    close: Decimal,
    high: Decimal,
    low: Decimal,
    offset_ms: int = BARS_OFFSET_MS,
    volume: str = "100",
    turnover: str = "100000",
) -> dict:
    return {
        "offset_ms": offset_ms,
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


def _write_replay(path: Path, *, level_price: Decimal, snapshot_cutoff_ts: int) -> dict:
    """写一段确定性回放：24 根平静柱 → 触及关键位 → 穿越画线 → 放量 →
    断流 → 跳过一分钟的恢复柱（制造 FEED_GAP）。"""

    start = (snapshot_cutoff_ts // 60_000 + 1) * 60_000
    calm = level_price - Decimal("200")
    events: list[dict] = [{"offset_ms": 0, "type": "heartbeat"}]

    for index in range(24):
        bar = _bar_event(
            instrument_id=INSTRUMENT,
            timestamp=start + index * 60_000,
            close=calm,
            high=calm + Decimal("10"),
            low=calm - Decimal("10"),
        )
        events.append(bar)

    touch = _bar_event(
        instrument_id=INSTRUMENT,
        timestamp=start + 24 * 60_000,
        close=level_price,
        high=level_price + Decimal("5"),
        low=level_price - Decimal("5"),
    )
    events.append(touch)

    cross = _bar_event(
        instrument_id=INSTRUMENT,
        timestamp=start + 25 * 60_000,
        close=level_price + Decimal("110"),
        high=level_price + Decimal("120"),
        low=level_price + Decimal("60"),
    )
    events.append(cross)

    follow = _bar_event(
        instrument_id=INSTRUMENT,
        timestamp=start + 26 * 60_000,
        close=level_price + Decimal("150"),
        high=level_price + Decimal("160"),
        low=level_price + Decimal("100"),
    )
    events.append(follow)

    spike = _bar_event(
        instrument_id=INSTRUMENT,
        timestamp=start + 27 * 60_000,
        close=level_price + Decimal("250"),
        high=level_price + Decimal("300"),
        low=level_price + Decimal("200"),
        volume="999999",
        turnover="99999900",
    )
    events.append(spike)

    events.append(
        {"offset_ms": DISCONNECT_OFFSET_MS, "type": "disconnect", "reason": "验收模拟行情断流"}
    )
    events.append({"offset_ms": RECOVERY_OFFSET_MS, "type": "reconnect"})

    recovery_ts = start + 29 * 60_000  # 跳过 28 号槽位，触发一根 FEED_GAP
    recovery = _bar_event(
        offset_ms=RECOVERY_OFFSET_MS,
        instrument_id=INSTRUMENT,
        timestamp=recovery_ts,
        close=level_price + Decimal("270"),
        high=level_price + Decimal("280"),
        low=level_price + Decimal("220"),
    )
    events.append(recovery)

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


def test_m1_solo_acceptance_flow(
    tmp_path: Path, fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text(
        "<!doctype html><title>Talaria</title>", encoding="utf-8"
    )

    # ------------------------------------------------------------------ 阶段 A
    with TestClient(
        create_app(_settings(data_dir, dist_dir, fixture_dir, now_ms=SNAPSHOT_CUTOFF_MS))
    ) as client:
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
                "cutoff_ts": SNAPSHOT_CUTOFF_MS,
            },
            headers=AUTH,
        )
        assert snapshot_resp.status_code == 200
        meta_a = snapshot_resp.json()
        snapshot_id = meta_a["snapshot_id"]
        assert meta_a["instrument_id"] == INSTRUMENT
        assert meta_a["cutoff_ts"] == SNAPSHOT_CUTOFF_MS
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
                "points": [{"ts": SNAPSHOT_CUTOFF_MS, "price": _decimal(drawing_price)}],
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
                    "version": "1", "timing": "event",
                    "expression": {"op": "level_event", "level_id": chosen['level_id'], "event": "TOUCH", "level_kind": chosen['kind']},
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
        assert level_alert["condition"]["expression"]["level_id"] == chosen["level_id"]

        overlay_alert_resp = client.post(
            "/api/alerts",
            json={
                "instrument_id": INSTRUMENT,
                "timeframe": TIMEFRAME,
                "condition": {
                    "version": "1", "timing": "event",
                    "expression": {"op": "overlay", "direction": "ABOVE", "drawing_id": drawing['drawing_id'], "mode": "TOUCH"},
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
        snapshot_cutoff_ts=meta_a["cutoff_ts"],
    )
    assert max(level_alert["created_ts"], overlay_alert["created_ts"]) <= replay["start"]
    replay_end_ms = replay["recovery_ts"] + 60_000

    clock = ReplayClock()
    monkeypatch.setattr(
        "talaria.workbench_api.runtime.ReplayFeed",
        partial(ReplayFeed, clock=clock.monotonic, sleep=clock.sleep),
    )
    app = create_app(_settings(data_dir, dist_dir, fixture_dir, replay_path, now_ms=replay_end_ms))
    with TestClient(app) as client:
        assert client.get("/api/health").json()["feed_mode"] == "replay"
        loaded = client.get("/api/alerts").json()["alerts"]
        assert {alert["alert_id"] for alert in loaded} >= {
            level_alert["alert_id"],
            overlay_alert["alert_id"],
        }

        # 重启时预热的版本晚于触及柱。按阶段 A 的截止时刻补冻，供历史回放选版。
        rebuild_resp = client.post(
            "/api/snapshots",
            json={
                "instrument_id": INSTRUMENT,
                "timeframes": [TIMEFRAME],
                "cutoff_ts": meta_a["cutoff_ts"],
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

            pump.wait_for(lambda m: m["type"] == "health" and m["data"]["status"] == "LIVE")
            client.portal.call(clock.advance_to, BARS_OFFSET_MS)
            pump.wait_for(
                lambda m: m["type"] == "bar" and m["data"]["bar"]["timestamp"] == replay["spike_ts"]
            )
            client.portal.call(app.state.runtime.alerts.flush)
            triggers = client.get(
                "/api/alerts/triggers", params={"instrument_id": INSTRUMENT, "limit": 50}
            ).json()["triggers"]
            assert {item["alert_id"] for item in triggers} >= {
                level_alert["alert_id"], overlay_alert["alert_id"]
            }
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
            assert Decimal(level_trigger["observed"]) == level_price
            assert overlay_trigger["condition_kind"] == "OVERLAY_CROSS"
            assert Decimal(overlay_trigger["threshold"]) == drawing_price
            assert level_trigger["snapshot_id"] and overlay_trigger["snapshot_id"]
            assert client.get(
                f"/api/snapshots/{level_trigger['snapshot_id']}"
            ).status_code == 200
            assert client.get(
                f"/api/snapshots/{overlay_trigger['snapshot_id']}"
            ).status_code == 200

            scan_resp = client.post(
                "/api/scanner/scan",
                json={"instrument_ids": [INSTRUMENT]},
                headers=AUTH,
            )
            assert scan_resp.status_code == 200
            # 定时扫描可能先命中同一根柱，手动扫描返回后从共同的触发历史读取。
            liquidity = client.get(
                "/api/scanner/triggers",
                params={"instrument_id": INSTRUMENT, "kind": "LIQUIDITY_ANOMALY"},
            ).json()["triggers"]
            assert len(liquidity) == 1
            assert liquidity[0]["kind"] == "LIQUIDITY_ANOMALY"
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

            client.portal.call(clock.advance_to, DISCONNECT_OFFSET_MS)
            pump.wait_for(lambda m: m["type"] == "health" and m["data"]["status"] == "STALE")
            assert client.get("/api/feed/health").json()["status"] == "STALE"
            client.portal.call(clock.advance_to, RECOVERY_OFFSET_MS)
            pump.wait_for(
                lambda m: m["type"] == "health" and m["data"]["status"] == "RECOVERING"
            )
            pump.wait_for(lambda m: m["type"] == "health" and m["data"]["status"] == "LIVE")
            assert client.get("/api/feed/health").json()["status"] == "LIVE"

            gap_frame = pump.wait_for(
                lambda message: message.get("type") == "bar"
                and message["data"]["bar"]["status"] == "FEED_GAP",
                timeout=10.0,
            )
            assert gap_frame["data"]["bar"]["close"] is None
            assert gap_frame["data"]["bar"]["timestamp"] == replay["gap_ts"]

            pump.wait_for(
                lambda m: m["type"] == "bar"
                and m["data"]["bar"]["timestamp"] == replay["recovery_ts"]
            )
            alert_frames = [
                message for message in pump.seen if message.get("type") == "alert"
            ]
            assert {frame["data"]["alert_id"] for frame in alert_frames} >= {
                level_alert["alert_id"],
                overlay_alert["alert_id"],
            }

    # ------------------------------------------------------------------ 阶段 C
    with TestClient(
        create_app(_settings(data_dir, dist_dir, fixture_dir, now_ms=replay_end_ms))
    ) as client:
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
