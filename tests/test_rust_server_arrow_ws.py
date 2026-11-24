"""End-to-end Python verification for the Rust `talaria-data` daemon:

- Process lifecycle (`cargo run --locked -p talaria-cli --bin talaria-data`),
  exclusive `service.lock`, `0o600` `service-descriptor.json` interop with
  `talaria.service_endpoint.read_service_connection`, Bearer + `X-Talaria-Data-Dir`
  guards with strict `{code, message}` errors, `/desk` SPA bootstrap & cookie,
  and clean `--exit-with-parent` shutdown.
- High-throughput Apache Arrow IPC streaming (`GET /api/bars.arrow`):
  transfers 1,000,000 `Decimal128` bars read via `pyarrow.ipc.open_stream` in
  `<= 100 ms` (steady state), verifies `Decimal128(18, s)` column scales, exact
  boundary values, manifest/hot-revision headers, and subrange slicing.
- Reliable WebSocket changelog replay (`WS /ws/strategy`) and desk stream
  (`WS /ws/desk`) via `websockets`: subscribes, receives monotonic `(epoch, seq)`
  confirmed kline events from `ReplayTransport`, drops connection abruptly,
  reconnects with `?from_epoch=&from_seq=`, and verifies missed events replay
  cleanly in sequence without gaps or duplicates.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import statistics
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator

import httpx
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
import websockets

from talaria.foundation.connector import RawBar
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.perception.cold_store import ColdBatch, ColdStore
from talaria.service_endpoint import DATA_DIR_HEADER, read_service_connection
from talaria.service_lock import probe_lock

pytestmark = [pytest.mark.medium, pytest.mark.spec]

REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR = REPO_ROOT / "engine"


def _seed_one_million_bars(data_dir: Path) -> tuple[int, int]:
    """Publish 1,000,000 BTC-USDT-PERP 1m bars partitioned by UTC month via ColdStore."""
    spec = CRYPTO_PERP_PACK.instruments()[0]
    store = ColdStore(data_dir)
    total = 0
    target = 1_000_000
    yr, mo = 2024, 1
    first_ts = int(datetime(yr, mo, 1, tzinfo=timezone.utc).timestamp() * 1000)
    last_ts = first_ts
    try:
        while total < target:
            m_start = int(datetime(yr, mo, 1, tzinfo=timezone.utc).timestamp() * 1000)
            nyr, nmo = (yr + 1, 1) if mo == 12 else (yr, mo + 1)
            m_end = int(datetime(nyr, nmo, 1, tzinfo=timezone.utc).timestamp() * 1000)
            count = min((m_end - m_start) // 60_000, target - total)
            bars = [
                RawBar(
                    symbol=spec.symbol,
                    timeframe="1m",
                    ts_event=m_start + i * 60_000,
                    open=Decimal("50000.1"),
                    high=Decimal("50001.2"),
                    low=Decimal("49999.0"),
                    close=Decimal("50000.5"),
                    volume=Decimal("1.234"),
                    turnover=Decimal("61700.617"),
                    ts_available=m_start + (i + 1) * 60_000,
                    ts_ingested=m_start + (i + 1) * 60_000,
                    ts_committed=m_start + (i + 1) * 60_000,
                )
                for i in range(count)
            ]
            store.publish(ColdBatch(spec, "1m", bars))
            last_ts = bars[-1].ts_event
            total += count
            yr, mo = nyr, nmo
    finally:
        store.close()
    assert total == target
    return first_ts, last_ts


@contextmanager
def _run_talaria_data(
    data_dir: Path,
    *,
    desk_dist: Path | None = None,
    replay_file: Path | None = None,
    replay_speed: float = 2.0,
) -> Iterator[tuple[dict[str, object], subprocess.Popen[bytes]]]:
    """Launch `talaria-data up` via `cargo run --locked` and verify clean parent-exit teardown."""
    cmd = [
        "cargo",
        "run",
        "--locked",
        "-p",
        "talaria-cli",
        "--bin",
        "talaria-data",
        "--",
        "up",
        "--data-dir",
        str(data_dir),
        "--port",
        "0",
        "--exit-with-parent",
    ]
    if desk_dist is not None:
        cmd.extend(["--desk-dist", str(desk_dist)])
    if replay_file is not None:
        cmd.extend(["--replay-file", str(replay_file), "--replay-speed", str(replay_speed)])

    env = os.environ.copy()
    env.setdefault("RUST_LOG", "warn")

    proc = subprocess.Popen(
        cmd,
        cwd=ENGINE_DIR,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    desc_path = data_dir / "runtime" / "service-descriptor.json"
    try:
        deadline = time.monotonic() + 120.0
        while not desc_path.is_file():
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                raise RuntimeError(f"talaria-data exited early ({proc.returncode}):\n{stderr}")
            assert time.monotonic() < deadline, "timed out waiting for service-descriptor.json"
            time.sleep(0.05)

        mode = stat.S_IMODE(desc_path.stat().st_mode)
        assert mode == 0o600, f"expected 0600 permissions on descriptor, got {oct(mode)}"
        desc = json.loads(desc_path.read_text(encoding="utf-8"))
        assert desc["base_url"].startswith("http://127.0.0.1:")

        conn = read_service_connection(data_dir)
        assert conn is not None, "Python service_endpoint must recognize Rust service.lock + descriptor"
        assert conn.base_url == desc["base_url"]
        assert conn.token == desc["token"]

        yield desc, proc
    finally:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        try:
            rc = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.terminate()
            rc = proc.wait(timeout=5)
        assert rc == 0, f"talaria-data did not exit cleanly on parent stdin close (rc={rc})"
        assert probe_lock(data_dir) is None, "service.lock must be released on clean exit"


def test_arrow_ipc_1m_bars_under_100ms_and_loopback_security(tmp_path: Path) -> None:
    data_dir = tmp_path / "cold_1m"
    data_dir.mkdir(parents=True)
    (data_dir / "service.json").write_text(
        json.dumps(
            {
                "version": 1,
                "features": {
                    "market_data": False,
                    "cold_store": True,
                    "alerts": False,
                    "scanner": False,
                },
            }
        ),
        encoding="utf-8",
    )
    first_ts, last_ts = _seed_one_million_bars(data_dir)

    dist_dir = tmp_path / "desk_dist"
    (dist_dir / "assets").mkdir(parents=True)
    (dist_dir / "index.html").write_text(
        "<!doctype html><html><body><div id='root'>Talaria Desk</div></body></html>",
        encoding="utf-8",
    )
    (dist_dir / "assets" / "app.js").write_text("console.log('talaria');\n", encoding="utf-8")

    with _run_talaria_data(data_dir, desk_dist=dist_dir) as (desc, _proc):
        base_url = str(desc["base_url"])
        token = str(desc["token"])
        data_dir_id = str(desc["data_dir_id"])

        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            health = client.get("/api/health")
            assert health.status_code == 200
            assert health.json()["ok"] is True
            assert health.headers.get(DATA_DIR_HEADER) == data_dir_id

            # Same-origin /desk SPA & token bootstrap
            desk_resp = client.get("/desk")
            assert desk_resp.status_code == 200
            assert "Talaria Desk" in desk_resp.text
            assert f"talaria_token={token}" in desk_resp.headers.get("set-cookie", "")
            assert client.get("/desk/assets/app.js").status_code == 200

            boot = client.get("/api/desk/bootstrap").json()
            assert boot["token"] == token
            assert boot["data_dir_id"] == data_dir_id

            # Auth + X-Talaria-Data-Dir error contract checks
            unauth = client.post("/api/watchlist/groups", json={"name": "swing"})
            assert unauth.status_code == 401
            assert set(unauth.json()) >= {"code", "message"}

            wrong_dir = client.get(
                "/api/health",
                headers={DATA_DIR_HEADER: "wrong-data-dir-id"},
            )
            assert wrong_dir.status_code == 401
            assert set(wrong_dir.json()) >= {"code", "message"}

            auth_ok = client.post(
                "/api/watchlist/groups",
                headers={"Authorization": f"Bearer {token}", DATA_DIR_HEADER: data_dir_id},
                json={"name": "swing"},
            )
            assert auth_ok.status_code == 200
            assert any(g["name"] == "swing" for g in auth_ok.json().get("groups", []))

        arrow_url = (
            f"{base_url}/api/bars.arrow?instrument=BTC-USDT-PERP&timeframe=1m"
            f"&start={first_ts}&end={last_ts}"
        )

        # Cold warmup + schema/data verification
        with urllib.request.urlopen(arrow_url, timeout=30.0) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("application/vnd.apache.arrow.stream")
            assert resp.headers.get("X-Talaria-Bars-Manifest")
            assert resp.headers.get("X-Talaria-Bars-Hot-Revision") == "none"
            payload = resp.read()

        table = ipc.open_stream(payload).read_all()
        assert table.num_rows == 1_000_000
        assert table.schema.field("open").type == pa.decimal128(18, 1)
        assert table.schema.field("high").type == pa.decimal128(18, 1)
        assert table.schema.field("low").type == pa.decimal128(18, 1)
        assert table.schema.field("close").type == pa.decimal128(18, 1)
        assert table.schema.field("volume").type == pa.decimal128(18, 3)
        assert table.schema.field("turnover").type == pa.decimal128(18, 4)
        assert table["ts_event"][0].as_py() == first_ts
        assert table["ts_event"][-1].as_py() == last_ts
        assert table["close"][0].as_py() == Decimal("50000.5")
        assert table["volume"][-1].as_py() == Decimal("1.234")

        # Steady-state 1M Decimal128 Arrow IPC transfer + PyArrow decode must be <= 100 ms
        durations_ms: list[float] = []
        for _ in range(3):
            t0 = time.perf_counter()
            with urllib.request.urlopen(arrow_url, timeout=30.0) as resp:
                raw = resp.read()
            loaded = ipc.open_stream(raw).read_all()
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            assert loaded.num_rows == 1_000_000
            durations_ms.append(elapsed_ms)

        assert min(durations_ms) <= 100.0, f"Arrow 1M transfer+decode too slow: {durations_ms}"
        assert statistics.median(durations_ms) <= 100.0, f"Arrow 1M median too slow: {durations_ms}"

        # Subrange slicing across month boundary
        sub_start = first_ts + 44_000 * 60_000
        sub_end = sub_start + 1_499 * 60_000
        sub_url = (
            f"{base_url}/api/bars.arrow?instrument=BTC-USDT-PERP&timeframe=1m"
            f"&start={sub_start}&end={sub_end}"
        )
        with urllib.request.urlopen(sub_url, timeout=30.0) as resp:
            sub_table = ipc.open_stream(resp.read()).read_all()
        assert sub_table.num_rows == 1500
        assert sub_table["ts_event"][0].as_py() == sub_start
        assert sub_table["ts_event"][-1].as_py() == sub_end


def test_strategy_ws_replay_and_desk_ws_with_websockets(tmp_path: Path) -> None:
    data_dir = tmp_path / "replay_ws"
    data_dir.mkdir(parents=True)
    replay_file = tmp_path / "events.jsonl"

    t0 = 1_704_067_200_000  # 2024-01-01T00:00:00Z
    replay_lines = [{"offset_ms": 0, "type": "heartbeat"}]
    offsets_ms = [1600, 2200, 5200, 6000]
    for idx, off in enumerate(offsets_ms):
        ts_ev = t0 + idx * 60_000
        close_px = f"{50100 + idx}.5"
        replay_lines.append(
            {
                "offset_ms": off,
                "type": "kline",
                "event": {
                    "instrument_id": "BTC-USDT-PERP",
                    "timeframe": "1m",
                    "confirmed": True,
                    "revision": 1,
                    "bar": {
                        "timestamp": ts_ev,
                        "open": "50000.0",
                        "high": "50200.0",
                        "low": "49950.0",
                        "close": close_px,
                        "volume": "12.500",
                        "turnover": "625000.000",
                        "status": "NORMAL",
                        "ts_available": ts_ev + 60_000,
                        "precision": "SECOND_VERIFIED",
                    },
                },
            }
        )
    replay_file.write_text(
        "\n".join(json.dumps(line) for line in replay_lines) + "\n",
        encoding="utf-8",
    )

    with _run_talaria_data(data_dir, replay_file=replay_file, replay_speed=2.0) as (desc, _proc):
        base_url = str(desc["base_url"])
        token = str(desc["token"])
        ws_base = base_url.replace("http://", "ws://", 1)

        async def _exercise_ws() -> None:
            headers = {"Authorization": f"Bearer {token}"}
            sub_req = json.dumps(
                {
                    "instrument": "BTC-USDT-PERP",
                    "timeframe": "1m",
                    "owner": "py-ws-strategy",
                }
            )

            # 1. Verify /ws/desk hello + health + subscribe
            async with websockets.connect(f"{ws_base}/ws/desk", open_timeout=5) as desk_ws:
                await desk_ws.send(
                    json.dumps(
                        {
                            "type": "subscribe",
                            "instrument": "BTC-USDT-PERP",
                            "timeframe": "1m",
                        }
                    )
                )
                first_desk = json.loads(await asyncio.wait_for(desk_ws.recv(), timeout=5))
                assert first_desk["type"] in {"hello", "health", "bar"}

            # 2. Connect WS #1 to /ws/strategy and receive first two bars
            seen_first: list[dict[str, object]] = []
            async with websockets.connect(
                f"{ws_base}/ws/strategy",
                additional_headers=headers,
                open_timeout=5,
            ) as ws1:
                await ws1.send(sub_req)
                ready1 = json.loads(await asyncio.wait_for(ws1.recv(), timeout=5))
                assert ready1["type"] == "ready"
                epoch = ready1["epoch"]

                while len(seen_first) < 2:
                    msg = json.loads(await asyncio.wait_for(ws1.recv(), timeout=8))
                    if msg.get("type") == "bar":
                        seen_first.append(msg)

                assert [m["bar"]["ts_event"] for m in seen_first] == [t0, t0 + 60_000]
                seq1, seq2 = seen_first[0]["seq"], seen_first[1]["seq"]
                assert isinstance(seq1, int) and isinstance(seq2, int)
                assert seq2 > seq1
                # Non-1000/1001 close retains the 30s reconnect lease on the server
                await ws1.close(code=4001, reason="simulated drop")

            # 3. Wait deterministically until bars #3 and #4 land in server storage
            with httpx.Client(base_url=base_url, timeout=10.0) as client:
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    r = client.get(
                        "/api/bars",
                        params={
                            "instrument": "BTC-USDT-PERP",
                            "timeframe": "1m",
                            "start": t0,
                            "end": t0 + 240_000,
                        },
                    )
                    if r.status_code == 200 and len(r.json().get("bars", [])) >= 4:
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("expected 4 replay bars to arrive via /api/bars")

            # 4. Reconnect with ?from_epoch=&from_seq= and verify ordered replay without gaps/dups
            reconnect_url = (
                f"{ws_base}/ws/strategy?from_epoch={epoch}&from_seq={seq2}"
            )
            replayed: list[dict[str, object]] = []
            async with websockets.connect(
                reconnect_url,
                additional_headers=headers,
                open_timeout=5,
            ) as ws2:
                await ws2.send(sub_req)
                ready2 = json.loads(await asyncio.wait_for(ws2.recv(), timeout=5))
                assert ready2["type"] == "ready"
                assert ready2["epoch"] == epoch

                while len(replayed) < 2:
                    msg = json.loads(await asyncio.wait_for(ws2.recv(), timeout=5))
                    if msg.get("type") == "bar":
                        replayed.append(msg)

            assert [m["bar"]["ts_event"] for m in replayed] == [
                t0 + 120_000,
                t0 + 180_000,
            ]
            assert [m["bar"]["close"] for m in replayed] == ["50102.5", "50103.5"]
            assert [m["seq"] for m in replayed] == [seq2 + 1, seq2 + 2]

        asyncio.run(_exercise_ws())
