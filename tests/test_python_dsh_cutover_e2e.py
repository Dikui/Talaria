"""End-to-end integration tests for Ticket 06:
Python SDK (Arrow IPC + DuckDB + WS /ws/strategy), `account/` cutover, and
downgraded `@talaria/dsh-plugin` connecting to the standalone Rust `talaria-data` engine.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import time
from contextlib import aclosing, closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator
from uuid import UUID

import httpx
import pyarrow as pa
import pytest
import websockets

from talaria import sdk
from talaria.account.paper import PaperInbox
from talaria.foundation.connector import RawBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.errors import InvalidRequestError
from talaria.foundation.strategy_subscription import SubscriptionCursor
from talaria.foundation.ticks import Quote, TickKind, Trade
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.perception.cold_store import ColdBatch, ColdStore
from talaria.service_endpoint import DATA_DIR_HEADER, read_service_connection
from talaria.service_lock import probe_lock

pytestmark = [pytest.mark.medium, pytest.mark.spec]

REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR = REPO_ROOT / "engine"
PLUGIN_DIR = REPO_ROOT / "dsh" / "packages" / "talaria-dsh-plugin"
MINUTE = 60_000


def _talaria_data_command(
    data_dir: Path,
    *,
    replay_file: Path | None = None,
    replay_speed: float = 20.0,
) -> tuple[list[str], Path]:
    debug_bin = ENGINE_DIR / "target" / "debug" / "talaria-data"
    if debug_bin.is_file() and os.access(debug_bin, os.X_OK):
        cmd = [str(debug_bin)]
        cwd = REPO_ROOT
    else:
        cmd = [
            "cargo",
            "run",
            "--locked",
            "-p",
            "talaria-cli",
            "--bin",
            "talaria-data",
            "--",
        ]
        cwd = ENGINE_DIR
    cmd.extend([
        "up",
        "--data-dir",
        str(data_dir),
        "--port",
        "0",
        "--exit-with-parent",
    ])
    if replay_file is not None:
        cmd.extend(["--replay-file", str(replay_file), "--replay-speed", str(replay_speed)])
    return cmd, cwd


@contextmanager
def _run_talaria_data(
    data_dir: Path,
    *,
    replay_file: Path | None = None,
    replay_speed: float = 20.0,
) -> Iterator[dict[str, object]]:
    cmd, cwd = _talaria_data_command(data_dir, replay_file=replay_file, replay_speed=replay_speed)
    env = os.environ.copy()
    env.setdefault("RUST_LOG", "warn")
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
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

        assert stat.S_IMODE(desc_path.stat().st_mode) == 0o600
        desc = json.loads(desc_path.read_text(encoding="utf-8"))
        conn = read_service_connection(data_dir)
        assert conn is not None and conn.base_url == desc["base_url"] and conn.token == desc["token"]
        yield desc
    finally:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        try:
            rc = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.terminate()
            rc = proc.wait(timeout=5)
        assert rc == 0
        assert probe_lock(data_dir) is None


def _seed_market_data(data_dir: Path) -> tuple[int, UUID, UUID]:
    btc = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
    eth = CRYPTO_PERP_PACK.instrument("ETH-USDT-PERP")
    assert btc is not None and eth is not None
    t0 = int(datetime(2026, 2, 1, 1, 0, tzinfo=timezone.utc).timestamp() * 1000)

    trade_id = UUID("11111111-2222-3333-4444-555555555555")
    conn_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    with closing(ColdStore(data_dir)) as store:
        btc_history = [
            RawBar(
                symbol=btc.symbol,
                timeframe="1m",
                ts_event=t0 - i * MINUTE,
                open=Decimal("50000.0"),
                high=Decimal("50005.0"),
                low=Decimal("49995.0"),
                close=Decimal("50001.0"),
                volume=Decimal("2.000"),
                turnover=Decimal("100002.00"),
                ts_available=t0 - (i - 1) * MINUTE,
                ts_ingested=t0 - (i - 1) * MINUTE,
                ts_committed=t0 - (i - 1) * MINUTE,
            )
            for i in range(18, 0, -1)
        ]
        store.publish(ColdBatch(
            btc,
            "1m",
            [
                *btc_history,
                RawBar(
                    symbol=btc.symbol,
                    timeframe="1m",
                    ts_event=t0,
                    open=Decimal("50000.1"),
                    high=Decimal("50005.0"),
                    low=Decimal("49995.0"),
                    close=Decimal("50001.5"),
                    volume=Decimal("2.500"),
                    turnover=Decimal("125003.75"),
                    ts_available=t0 + MINUTE,
                    ts_ingested=t0 + MINUTE,
                    ts_committed=t0 + MINUTE,
                ),
                RawBar(
                    symbol=btc.symbol,
                    timeframe="1m",
                    ts_event=t0 + MINUTE,
                    open=Decimal("50001.5"),
                    high=Decimal("50010.0"),
                    low=Decimal("50000.0"),
                    close=Decimal("50008.2"),
                    volume=Decimal("1.500"),
                    turnover=Decimal("75012.30"),
                    ts_available=t0 + 2 * MINUTE,
                    ts_ingested=t0 + 2 * MINUTE,
                    ts_committed=t0 + 2 * MINUTE,
                    revision=0,
                ),
            ],
        ))
        store.publish(ColdBatch(
            eth,
            "1m",
            [
                RawBar(
                    symbol=eth.symbol,
                    timeframe="1m",
                    ts_event=t0,
                    open=Decimal("3000.10"),
                    high=Decimal("3005.00"),
                    low=Decimal("2998.00"),
                    close=Decimal("3002.25"),
                    volume=Decimal("10.000"),
                    turnover=Decimal("30022.50"),
                    ts_available=t0 + MINUTE,
                    ts_ingested=t0 + MINUTE,
                    ts_committed=t0 + MINUTE,
                ),
                RawBar(
                    symbol=eth.symbol,
                    timeframe="1m",
                    ts_event=t0 + MINUTE,
                    open=Decimal("3002.25"),
                    high=Decimal("3010.00"),
                    low=Decimal("3001.00"),
                    close=Decimal("3009.75"),
                    volume=Decimal("8.000"),
                    turnover=Decimal("24078.00"),
                    ts_available=t0 + 2 * MINUTE,
                    ts_ingested=t0 + 2 * MINUTE,
                    ts_committed=t0 + 2 * MINUTE,
                ),
            ],
        ))
        store.publish_ticks(
            btc,
            TickKind.TRADES,
            [
                Trade(
                    instrument_id=btc.instrument_id,
                    trade_id=trade_id,
                    side="Buy",
                    price=Decimal("50001.5"),
                    size=Decimal("0.250"),
                    ts_event=t0 + 15_000,
                    ts_available=t0 + 15_000,
                    ts_ingested=t0 + 15_010,
                    ts_committed=t0 + 15_020,
                )
            ],
        )
        store.publish_ticks(
            btc,
            TickKind.QUOTES,
            [
                Quote(
                    instrument_id=btc.instrument_id,
                    bid_price=Decimal("50001.4"),
                    bid_size=Decimal("1.200"),
                    ask_price=Decimal("50001.6"),
                    ask_size=Decimal("0.800"),
                    connection_id=conn_id,
                    update_id=42,
                    ts_event=t0 + 15_000,
                    ts_available=t0 + 15_000,
                    ts_ingested=t0 + 15_005,
                    ts_committed=t0 + 15_010,
                )
            ],
        )
        store.publish_derivatives(
            btc,
            DerivativeKind.MARK_PRICE,
            [
                DerivativePoint(
                    instrument_id=btc.instrument_id,
                    kind=DerivativeKind.MARK_PRICE,
                    timeframe="1m",
                    value=Decimal("50002.0"),
                    ts_event=t0,
                    ts_available=t0 + MINUTE,
                    ts_ingested=t0 + MINUTE,
                    ts_committed=t0 + MINUTE,
                )
            ],
        )
        store.publish_derivatives(
            btc,
            DerivativeKind.FUNDING_RATE,
            [
                DerivativePoint(
                    instrument_id=btc.instrument_id,
                    kind=DerivativeKind.FUNDING_RATE,
                    timeframe=None,
                    value=Decimal("0.00012500"),
                    ts_event=t0,
                    ts_published=t0,
                    settled=True,
                    ts_available=t0 + 5_000,
                    ts_ingested=t0 + 5_000,
                    ts_committed=t0 + 5_010,
                )
            ],
        )
    return t0, trade_id, conn_id


def _replay_line(
    offset_ms: int,
    *,
    ts_event: int,
    close: str,
    revision: int = 0,
    confirmed: bool = True,
    timeframe: str = "1m",
) -> str:
    return json.dumps({
        "offset_ms": offset_ms,
        "type": "kline",
        "event": {
            "instrument_id": "BTC-USDT-PERP",
            "timeframe": timeframe,
            "confirmed": confirmed,
            "revision": revision,
            "bar": {
                "timestamp": ts_event,
                "open": "50010.0",
                "high": "50020.0",
                "low": "50005.0",
                "close": close,
                "volume": "3.000",
                "turnover": "150030.0",
                "status": "NORMAL",
                "ts_available": ts_event + MINUTE,
                "precision": "SECOND_VERIFIED",
            },
        },
    })


def _write_service_config(data_dir: Path, *, market_data: bool = False) -> None:
    (data_dir / "service.json").write_text(
        json.dumps({
            "version": 1,
            "features": {
                "market_data": market_data,
                "cold_store": True,
                "alerts": True,
                "scanner": True,
            },
        }),
        encoding="utf-8",
    )


def test_sdk_duckdb_readonly_and_asof_join_over_cold_store(tmp_path: Path) -> None:
    data_dir = tmp_path / "duckdb_data"
    data_dir.mkdir(parents=True)
    t0, _, _ = _seed_market_data(data_dir)

    btc = sdk.bars("BTC-USDT-PERP", "1m", t0, t0 + MINUTE, data_dir=data_dir, offline=True)
    eth = sdk.bars("ETH-USDT-PERP", "1m", t0, t0 + MINUTE, data_dir=data_dir, offline=True)
    mark = sdk.derivatives(
        "BTC-USDT-PERP", "mark_price", t0, t0 + MINUTE, as_of=t0 + 2 * MINUTE, data_dir=data_dir
    )

    btc_arrow = btc.to_arrow()
    assert pa.types.is_decimal(btc_arrow.schema.field("close").type)

    summary = btc.query_duckdb("SELECT COUNT(*) AS n, MAX(close) AS max_close FROM bars")
    assert int(summary.loc[0, "n"]) == 2
    assert Decimal(str(summary.loc[0, "max_close"])) == Decimal("50008.2")

    joined = btc.asof_join(eth, on="ts_event", right_prefix="eth_", tolerance_ms=0, as_arrow=True)
    assert isinstance(joined, pa.Table)
    assert [row["eth_close"] for row in joined.to_pylist()] == [
        Decimal("3002.25"),
        Decimal("3009.75"),
    ]

    with_mark = btc.asof_join(
        mark, on="ts_event", right_columns=("value",), right_prefix="mark_", tolerance_ms=MINUTE
    )
    assert list(with_mark["mark_value"].astype(float)) == [50002.0, 50002.0]

    paths = sdk.cold_parquet_paths("BTC-USDT-PERP", "1m", data_dir=data_dir)
    assert len(paths) == 1
    manifest_df = sdk.query_duckdb(
        "SELECT dataset, symbol, rows FROM talaria_manifest WHERE dataset = 'bars' ORDER BY symbol",
        data_dir=data_dir,
    )
    assert list(manifest_df["symbol"]) == ["BTCUSDT", "ETHUSDT"]

    parquet_sql = f"SELECT ts_event, close FROM read_parquet('{paths[0].as_posix()}') ORDER BY ts_event"
    parquet_df = sdk.query_duckdb(parquet_sql, data_dir=data_dir)
    assert len(parquet_df) == 20

    with pytest.raises(InvalidRequestError):
        sdk.query_duckdb("CREATE TABLE illegal (x INT)")
    with pytest.raises(InvalidRequestError):
        sdk.query_duckdb(f"ATTACH '{(tmp_path / 'bad.duckdb').as_posix()}' AS db")

    assert list(tmp_path.rglob("*.duckdb")) == []


def test_sdk_arrow_ipc_cold_hot_splicing_ticks_derivatives_and_account_cutover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "sdk_live"
    data_dir.mkdir(parents=True)
    _write_service_config(data_dir, market_data=True)
    t0, trade_id, conn_id = _seed_market_data(data_dir)

    replay_file = tmp_path / "replay_tail.jsonl"
    hot_ts = t0 + 2 * MINUTE
    replay_file.write_text(
        "\n".join([
            json.dumps({"offset_ms": 0, "type": "heartbeat"}),
            _replay_line(3000, ts_event=t0 + MINUTE, close="50009.0", revision=0),
            _replay_line(3400, ts_event=t0 + MINUTE, close="50009.5", revision=1),
            _replay_line(3800, ts_event=hot_ts, close="50015.5", revision=0),
        ])
        + "\n",
        encoding="utf-8",
    )

    seen_paths: list[str] = []
    real_get = httpx.Client.get

    def spy_get(self, url, *args, **kwargs):
        seen_paths.append(str(url))
        return real_get(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "get", spy_get)

    with _run_talaria_data(data_dir, replay_file=replay_file, replay_speed=2.0) as desc:
        base_url = str(desc["base_url"])
        token = str(desc["token"])
        data_dir_id = str(desc["data_dir_id"])
        ws_base = base_url.replace("http://", "ws://", 1)

        async def _subscribe_and_wait_hot() -> None:
            async with websockets.connect(
                f"{ws_base}/ws/strategy",
                additional_headers={"Authorization": f"Bearer {token}"},
                open_timeout=5,
            ) as strat_ws:
                await strat_ws.send(json.dumps({
                    "instrument": "BTC-USDT-PERP",
                    "timeframe": "1m",
                    "owner": "e2e-arrow-watcher",
                }))
                ready = json.loads(await asyncio.wait_for(strat_ws.recv(), timeout=5))
                assert ready["type"] == "ready"
                deadline = time.monotonic() + 10.0
                last_rows: list[dict[str, object]] = []
                with httpx.Client(base_url=base_url, timeout=10.0) as client:
                    while time.monotonic() < deadline:
                        bars_resp = client.get(
                            "/api/bars.arrow",
                            params={
                                "instrument": "BTC-USDT-PERP",
                                "timeframe": "1m",
                                "start": t0,
                                "end": hot_ts,
                            },
                        )
                        if bars_resp.status_code == 200:
                            stream = pa.ipc.open_stream(bars_resp.content).read_all()
                            last_rows = stream.to_pylist()
                            if len(last_rows) == 3 and last_rows[1]["close"] == Decimal("50009.5"):
                                return
                        await asyncio.sleep(0.05)
                raise AssertionError(
                    "timed out waiting for hot tail bars on talaria-data: "
                    f"{[(r['ts_event'], str(r['close']), r['revision']) for r in last_rows]}"
                )

        asyncio.run(_subscribe_and_wait_hot())

        with httpx.Client(base_url=base_url, timeout=10.0) as client:
            result = sdk.bars(
                "BTC-USDT-PERP",
                "1m",
                t0,
                hot_ts,
                data_dir=data_dir,
            )
            assert result.tail_completed is True
            assert [(b.ts_event, b.close) for b in result] == [
                (t0, Decimal("50001.5")),
                (t0 + MINUTE, Decimal("50009.5")),
                (hot_ts, Decimal("50015.5")),
            ]

            trades = sdk.trades(
                "BTC-USDT-PERP", t0, t0 + MINUTE, as_of=t0 + MINUTE, data_dir=data_dir
            )
            assert len(trades) == 1
            assert trades[0].trade_id == trade_id
            assert trades[0].price == Decimal("50001.5")

            quotes = sdk.quotes(
                "BTC-USDT-PERP", t0, t0 + MINUTE, as_of=t0 + MINUTE, data_dir=data_dir
            )
            assert len(quotes) == 1
            assert quotes[0].connection_id == conn_id
            assert quotes[0].ask_price == Decimal("50001.6")

            marks = sdk.derivatives(
                "BTC-USDT-PERP",
                "mark_price",
                t0,
                t0 + MINUTE,
                as_of=t0 + 2 * MINUTE,
                data_dir=data_dir,
            )
            assert len(marks) == 1 and marks[0].value == Decimal("50002.0")

            settlements = sdk.funding_settlements(
                "BTC-USDT-PERP",
                t0,
                t0 + MINUTE,
                as_of=t0 + 2 * MINUTE,
                data_dir=data_dir,
            )
            assert len(settlements) == 1 and settlements[0].value == Decimal("0.00012500")

            # Indicator defaults updated on talaria-data persist in state/workbench_state.sqlite
            put_defaults = client.put(
                "/api/indicator-defaults/EMA",
                headers={"Authorization": f"Bearer {token}", DATA_DIR_HEADER: data_dir_id},
                json={"period": 21},
            )
            assert put_defaults.status_code == 200
            defaults = sdk.indicator_defaults(data_dir=data_dir)
            assert defaults["EMA"].period == 21
            assert (data_dir / "state" / "workbench_state.sqlite").is_file()

            # PaperInbox handles standalone talaria-data without crashing on missing /api/paper/*
            inbox = PaperInbox(data_dir, ["sa-1"], client)
            assert inbox.claim("sa-1", "BTC-USDT-PERP", hot_ts) is None

    assert "/api/bars.arrow" in seen_paths
    assert "/api/ticks.arrow" in seen_paths
    assert "/api/derivatives.arrow" in seen_paths


def test_sdk_subscribe_ws_strategy_resume_and_1013_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "ws_data"
    data_dir.mkdir(parents=True)
    _write_service_config(data_dir, market_data=True)
    t0 = int(datetime(2026, 2, 2, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    replay_file = tmp_path / "ws_replay.jsonl"
    replay_file.write_text(
        "\n".join([
            json.dumps({"offset_ms": 0, "type": "heartbeat"}),
            _replay_line(1600, ts_event=t0, close="50100.5", revision=1),
            _replay_line(2200, ts_event=t0 + MINUTE, close="50101.5", revision=1),
            _replay_line(5200, ts_event=t0 + 2 * MINUTE, close="50102.5", revision=1),
            _replay_line(6000, ts_event=t0 + 3 * MINUTE, close="50103.5", revision=1),
        ])
        + "\n",
        encoding="utf-8",
    )

    with _run_talaria_data(data_dir, replay_file=replay_file, replay_speed=2.0):
        async def _verify_ws() -> None:
            from talaria.sdk import _subscription
            from websockets.exceptions import ConnectionClosedError
            from websockets.frames import Close

            real_connect = _subscription.connect
            injected = False

            class _DropAfterTwoWith1013:
                def __init__(self, args, kwargs):
                    self._inner = real_connect(*args, **kwargs)
                    self._ws = None
                    self._bars_seen = 0
                    self._active = False

                async def __aenter__(self):
                    nonlocal injected
                    self._ws = await self._inner.__aenter__()
                    self._active = not injected
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return await self._inner.__aexit__(exc_type, exc, tb)

                async def send(self, msg):
                    return await self._ws.send(msg)

                async def close(self, **kwargs):
                    return await self._ws.close(**kwargs)

                async def recv(self):
                    nonlocal injected
                    raw = await self._ws.recv()
                    if self._active and not injected and '"type":"bar"' in raw:
                        self._bars_seen += 1
                        if self._bars_seen == 3:
                            # Simulate Rust queue-overflow Close(1013) before delivering bar 3,
                            # forcing reconnect with saved (epoch, seq2) to replay bar 3 & bar 4.
                            injected = True
                            await self._ws.close(code=4001, reason="retain reconnect lease")
                            raise ConnectionClosedError(Close(1013, "consumer queue overflow"), None)
                    return raw

            monkeypatch.setattr(
                _subscription,
                "connect",
                lambda *args, **kwargs: _DropAfterTwoWith1013(args, kwargs),
            )

            cursors: list[tuple[SubscriptionCursor, int]] = []
            states: list[bool] = []
            received = []

            async with aclosing(
                sdk.subscribe(
                    "BTC-USDT-PERP",
                    "1m",
                    data_dir=data_dir,
                    on_connection=states.append,
                    on_cursor=lambda c, s: cursors.append((c, s)),
                )
            ) as stream:
                while len(received) < 4:
                    ev = await asyncio.wait_for(anext(stream), timeout=15.0)
                    received.append(ev)

            assert injected is True
            assert [ev.bar.ts_event for ev in received] == [
                t0,
                t0 + MINUTE,
                t0 + 2 * MINUTE,
                t0 + 3 * MINUTE,
            ]
            assert [ev.bar.close for ev in received] == [
                Decimal("50100.5"),
                Decimal("50101.5"),
                Decimal("50102.5"),
                Decimal("50103.5"),
            ]
            assert [ev.seq for ev in received] == [1, 2, 3, 4]
            assert len({ev.epoch for ev in received}) == 1
            assert states == [True, False, True]

            # Stale epoch triggers `resync` recovery via /api/bars?resync_from=...
            resynced = []
            async with aclosing(
                sdk.subscribe(
                    "BTC-USDT-PERP",
                    "1m",
                    data_dir=data_dir,
                    after=SubscriptionCursor(epoch="stale-epoch", seq=99),
                )
            ) as stream:
                while len(resynced) < 4:
                    ev = await asyncio.wait_for(anext(stream), timeout=10.0)
                    resynced.append(ev)

            assert [ev.bar.ts_event for ev in resynced] == [
                t0,
                t0 + MINUTE,
                t0 + 2 * MINUTE,
                t0 + 3 * MINUTE,
            ]

        asyncio.run(_verify_ws())


def test_dsh_plugin_bridge_against_absent_and_live_talaria_data(tmp_path: Path) -> None:
    node = shutil.which("node") or "/opt/homebrew/bin/node"
    assert Path(node).is_file(), "node executable required for DSH plugin integration test"

    absent_dir = tmp_path / "absent_data"
    absent_dir.mkdir(parents=True)
    bridge_url = (PLUGIN_DIR / "bridge.js").as_uri()

    absent_script = f"""
    import assert from 'node:assert/strict';
    import {{ BridgeError, createBridge }} from {json.dumps(bridge_url)};
    const bridge = createBridge({{ dataDir: {json.dumps(str(absent_dir))} }});
    await assert.rejects(
      () => bridge.createSnapshot({{ instrument_id: 'BTC-USDT-PERP', timeframes: ['1m'] }}),
      (err) => err instanceof BridgeError
        && err.code === 'TALARIA_BRIDGE_UNAVAILABLE'
        && /service-descriptor\\.json/.test(err.message)
        && /talaria-data up/.test(err.message)
        && /Talaria 数据引擎未运行/.test(err.message)
    );
    """
    absent_res = subprocess.run(
        [node, "--input-type=module", "-e", absent_script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert absent_res.returncode == 0, absent_res.stderr

    live_dir = tmp_path / "live_dsh_data"
    live_dir.mkdir(parents=True)
    _write_service_config(live_dir)
    t0, _, _ = _seed_market_data(live_dir)

    with _run_talaria_data(live_dir) as desc:
        live_script = f"""
        import assert from 'node:assert/strict';
        import {{ createBridge }} from {json.dumps(bridge_url)};
        const bridge = createBridge({{ dataDir: {json.dumps(str(live_dir))} }});
        const cred = bridge.credential();
        assert.equal(cred.base_url, {json.dumps(str(desc['base_url']))});
        assert.equal(cred.data_dir_id, {json.dumps(str(desc['data_dir_id']))});

        const snap = await bridge.createSnapshot({{
          instrument_id: 'BTC-USDT-PERP',
          timeframes: ['1m'],
          cutoff_ts: {t0 + 2 * MINUTE},
          mode: 'HISTORICAL_PIT',
        }});
        assert.ok(snap.snapshot_id);

        const ind = await bridge.getIndicators({{
          snapshot_id: snap.snapshot_id,
          instrument_id: 'BTC-USDT-PERP',
          timeframe: '1m',
          names: ['EMA'],
        }});
        assert.equal(ind.snapshot_id, snap.snapshot_id);

        const levels = await bridge.getKeyLevels({{
          snapshot_id: snap.snapshot_id,
          instrument_id: 'BTC-USDT-PERP',
          timeframe: '1m',
        }});
        assert.equal(levels.snapshot_id, snap.snapshot_id);

        const op = await bridge.computeOperator({{
          snapshot_id: snap.snapshot_id,
          timeframe: '1m',
          operator: 'EMA',
          params: {{ period: 5 }},
        }});
        assert.equal(op.snapshot_id, snap.snapshot_id);

        const view = await bridge.getChartView({{
          snapshot_id: snap.snapshot_id,
          instrument_id: 'BTC-USDT-PERP',
          timeframe: '1m',
          from_ts: {t0},
          to_ts: {t0 + 2 * MINUTE},
          price_min: '49000.0',
          price_max: '51000.0',
        }});
        assert.equal(view.snapshot_id, snap.snapshot_id);

        const ann = await bridge.writeAgentOverlay({{
          snapshot_id: snap.snapshot_id,
          instrument_id: 'BTC-USDT-PERP',
          timeframe: '1m',
          session_id: 'dsh-consult-e2e',
          round: 1,
          kind: 'LEVEL',
          label: '支撑位',
          rationale: '冷库实测 K 线低点证据',
          price_low: '50001.5',
          price_high: '50001.5',
          ts_anchor: {t0},
          evidence_refs: [{{ kline_ts: {t0}, note: 'bar evidence' }}],
        }});
        assert.equal(ann.status, 'ACTIVE');
        assert.equal(ann.snapshot_id, snap.snapshot_id);

        const alert = await bridge.createAlertDraft({{
          snapshot_id: snap.snapshot_id,
          instrument_id: 'BTC-USDT-PERP',
          timeframe: '1m',
          note: 'E2E draft alert',
          condition: {{
            version: '1',
            timing: 'event',
            expression: {{
              op: 'compare',
              test: 'gte',
              left: {{ source: 'event', metric: 'last_price' }},
              right: {{ source: 'constant', value: '100000' }},
            }},
          }},
        }});
        assert.equal(alert.status, 'DRAFT');
        """
        live_res = subprocess.run(
            [node, "--input-type=module", "-e", live_script],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert live_res.returncode == 0, live_res.stderr

        with httpx.Client(base_url=str(desc["base_url"]), timeout=5.0) as client:
            listed = client.get("/api/annotations", params={"instrument_id": "BTC-USDT-PERP"})
            assert listed.status_code == 200
            assert len(listed.json()["annotations"]) == 1
            assert listed.json()["annotations"][0]["label"] == "支撑位"
