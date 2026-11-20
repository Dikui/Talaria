"""协同会诊 API：快通道冻结、会诊包、Abstain、宿主回调与 Token 幂等。"""

from __future__ import annotations

import time
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from talaria.cli import main as cli_main
from talaria.foundation.connector import RawBar
from talaria.foundation.market import (
    CarryTrait,
    FeedCapabilityTrait,
    InstrumentSpec,
    MarginTrait,
    OrderConstraintTrait,
    SessionTrait,
    TraitBundle,
)
from talaria.packs import MARKET_PACKS
from talaria.replay.replay import ReplayFeed
from talaria.workbench_api.app import create_app
from talaria.workbench_api.settings import AppSettings

pytestmark = pytest.mark.medium

TOKEN = "test-token"
NOW_MS = 1_795_022_974_103
AUTH = {"Authorization": f"Bearer {TOKEN}"}
INSTRUMENT = "BTC-USDT-PERP"


def _settings(tmp_path: Path, fixture_dir: Path, *, dist: Path) -> AppSettings:
    return AppSettings(
        data_dir=tmp_path / "data",
        dist_dir=dist,
        fixture_dir=fixture_dir,
        token=TOKEN,
        now_ms=lambda: NOW_MS,
    )


@pytest.fixture
def dist_dir(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Talaria</title>", encoding="utf-8")
    return dist


@pytest.fixture
def client_factory(tmp_path, fixture_dir, dist_dir):
    clients: list[TestClient] = []

    def build(**kwargs) -> TestClient:
        app = create_app(
            _settings(tmp_path, fixture_dir, dist=dist_dir),
            collaboration_host=kwargs.pop("collaboration_host", None),
            **kwargs,
        )
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(client_factory) -> TestClient:
    return client_factory()


def _create(client: TestClient, *, cutoff_ts: int | None = None) -> str:
    body: dict[str, object] = {"instrument_id": INSTRUMENT, "timeframes": ["1m"]}
    if cutoff_ts is not None:
        body["cutoff_ts"] = cutoff_ts
    response = client.post("/api/snapshots", json=body, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()["snapshot_id"]


def _consult(client: TestClient, snapshot_id: str, **overrides):
    body = {
        "snapshot_id": snapshot_id,
        "instrument_id": INSTRUMENT,
        "timeframe": "1m",
        "question": "现在适合等待回踩还是突破？",
        "visible": {
            "from_ts": NOW_MS - 3_600_000,
            "to_ts": NOW_MS,
            "price_min": "0",
            "price_max": "10000000",
        },
        "drawing_ids": [],
    }
    body.update(overrides)
    return client.post("/api/collaboration/consult", json=body, headers=AUTH)


def _wait(client: TestClient, consultation_id: str, status: str, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/collaboration/consultations/{consultation_id}")
        assert response.status_code == 200
        last = response.json()
        if last["status"] == status:
            return last
        time.sleep(0.02)
    raise AssertionError(f"会诊未进入 {status}：{last}")


def _install_host(client: TestClient, captured: list[dict]) -> None:
    service = client.app.state.runtime.collaborations
    service._host_url = lambda: "http://127.0.0.1:65530"

    async def dispatcher(url: str, payload) -> None:
        captured.append({"url": url, "payload": dict(payload)})

    service._dispatcher = dispatcher


def _insight_text(snapshot_id: str) -> str:
    return (
        "```json\n"
        + "{"
        + '"direction":"LONG",'
        + '"summary":"等待回踩后观察承接",'
        + '"entry_low":"60000",'
        + '"entry_high":"60100",'
        + '"invalidation_stop":"59000",'
        + '"take_profit":["62000"],'
        + '"max_position":"0.2",'
        + f'"evidence_refs":[{{"evidence_id":"snapshot:{snapshot_id}","kind":"SNAPSHOT","label":"快照证据"}}]'
        + "}\n```"
    )


def test_consult_requires_bearer_token(client: TestClient) -> None:
    response = client.post(
        "/api/collaboration/consult",
        json={
            "snapshot_id": "snap-x",
            "instrument_id": INSTRUMENT,
            "timeframe": "1m",
            "question": "测试",
            "visible": {
                "from_ts": 1,
                "to_ts": 2,
                "price_min": "1",
                "price_max": "2",
            },
        },
    )
    assert response.status_code == 401


def test_stale_snapshot_abstains_without_dispatch(client: TestClient) -> None:
    snapshot_id = _create(client, cutoff_ts=NOW_MS - 3_600_000)
    response = _consult(client, snapshot_id)
    assert response.status_code == 202
    body = response.json()
    assert body["snapshot_id"] == snapshot_id
    record = _wait(client, body["consultation_id"], "ABSTAIN")
    assert record["abstain"]["reason_code"] == "STALE_SNAPSHOT"
    assert record["abstain"]["message"]
    assert record["package"]["catalysts"] == []


def test_host_offline_abstains_quickly(client: TestClient) -> None:
    snapshot_id = _create(client)
    body = _consult(client, snapshot_id).json()
    record = _wait(client, body["consultation_id"], "ABSTAIN")
    assert record["abstain"]["reason_code"] == "HOST_OFFLINE"
    assert "快通道" in record["abstain"]["message"]


def test_host_timeout_abstains(client: TestClient) -> None:
    snapshot_id = _create(client)
    service = client.app.state.runtime.collaborations
    _install_host(client, [])
    service._overall_timeout_s = 0.05

    async def hanging(url: str, payload) -> None:
        return None

    service._dispatcher = hanging
    body = _consult(client, snapshot_id).json()
    record = _wait(client, body["consultation_id"], "ABSTAIN")
    assert record["abstain"]["reason_code"] == "MODEL_TIMEOUT"


def test_host_result_becomes_ready_and_prompt_has_three_sections(
    client: TestClient,
) -> None:
    snapshot_id = _create(client)
    captured: list[dict] = []
    _install_host(client, captured)
    body = _consult(client, snapshot_id).json()
    deadline = time.monotonic() + 2
    while not captured and time.monotonic() < deadline:
        time.sleep(0.02)
    assert captured, "会诊没有派发到宿主"
    prompt = captured[0]["payload"]["prompt"]
    assert prompt.index("静态分析纪律") < prompt.index("加密永续 Market Pack")
    assert prompt.index("加密永续 Market Pack") < prompt.index("当前快照增量")
    assert snapshot_id in prompt

    response = client.post(
        f"/api/collaboration/consultations/{body['consultation_id']}/result",
        json={
            "text": _insight_text(snapshot_id),
            "dsh_session_id": "session-1",
            "dsh_turn_id": "7",
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["status"] == "READY", record.get("abstain") or record
    assert record["insight"]["direction"] == "LONG"
    assert record["dsh_session_id"] == "session-1"
    assert record["dsh_turn_id"] == "7"


# 任务单 04：用假市场的数据源和领域技能贯通 CLI、工作台与宿主派发。
@pytest.mark.spec
def test_up_uses_selected_market_pack(tmp_path, dist_dir, monkeypatch) -> None:
    spec = InstrumentSpec(
        instrument_id="SAMPLE.TEST",
        venue="test",
        symbol="SAMPLE",
        asset_class="stock",
        tick_size="0.01",
        qty_step="1",
        min_qty="1",
        price_precision=2,
        qty_precision=0,
    )
    cutoff = int(time.time() // 60) * 60_000
    price = Decimal("101.25")
    bar = RawBar(
        symbol=spec.symbol,
        timeframe="1m",
        ts_event=cutoff - 60_000,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("1"),
        turnover=price,
        ts_available=cutoff,
        ts_ingested=cutoff,
        ts_committed=cutoff,
    )
    events = tmp_path / "heartbeat.jsonl"
    events.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")

    class SampleHistory:
        def fetch_bars(self, *, symbol, timeframe, end_ts=None, limit=1000):
            if (symbol, timeframe) == (bar.symbol, bar.timeframe) and limit > 0:
                if end_ts is None or bar.ts_event <= end_ts:
                    return [bar]
            return []

    class SampleStream(ReplayFeed):
        feed_mode = "sample-live"

    skill = "# 第二段 · 样本市场领域技能\n\n观察盘中成交量。"

    class SamplePack:
        name = "sample"
        raw_archive_id = "sample_raw"
        market_skill = skill
        default_fixture_dir = None

        def instruments(self):
            return (spec,)

        def instrument(self, instrument_id):
            return spec if instrument_id == spec.instrument_id else None

        def traits(self, instrument_id):
            if self.instrument(instrument_id) is None:
                raise KeyError(instrument_id)
            return TraitBundle(
                feed=FeedCapabilityTrait(realtime=True, polling=True, history=True),
                margin=MarginTrait(margin_asset="USD", max_leverage=Decimal("1")),
                carry=CarryTrait(funding_interval_hours=None),
                session=SessionTrait(session_kind="regular"),
                order_constraint=OrderConstraintTrait(post_only=False),
            )

        def create_history_connector(self, *, base_url=None):
            return SampleHistory()

        def create_realtime_stream(self, *, now_ms):
            return SampleStream(events)

    monkeypatch.setitem(MARKET_PACKS, SamplePack.name, SamplePack())
    apps = []

    def capture_app(server) -> None:
        # 用 TestClient 运行真实 lifespan，无需绑定回环端口。
        apps.append(server.config.app)
        server.started = True

    monkeypatch.setattr(cli_main.uvicorn.Server, "run", capture_app)
    data_dir = tmp_path / "data"
    assert cli_main.main([
        "up", "--solo", "--pack", "sample", "--data-dir", str(data_dir),
        "--dist-dir", str(dist_dir), "--backfill-bars", "1",
    ]) == 0
    with TestClient(apps[0]) as client:
        assert client.get("/api/instruments").json() == [spec.model_dump(mode="json")]
        assert client.get("/api/health").json()["feed_mode"] == "sample-live"
        headers = {"Authorization": f"Bearer {client.app.state.token}"}
        snapshot = client.post(
            "/api/snapshots", headers=headers,
            json={"instrument_id": spec.instrument_id, "timeframes": ["1m"]},
        )
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["bar_counts"] == {"1m": 1}
        assert (data_dir / "data/raw/sample_raw/SAMPLE/1m.jsonl").is_file()

        captured: list[dict] = []
        _install_host(client, captured)
        response = client.post(
            "/api/collaboration/consult", headers=headers,
            json={
                "snapshot_id": snapshot.json()["snapshot_id"],
                "instrument_id": spec.instrument_id,
                "timeframe": "1m",
                "question": "盘中成交量如何？",
                "visible": {
                    "from_ts": cutoff - 60_000, "to_ts": cutoff,
                    "price_min": "90", "price_max": "110",
                },
            },
        )
        assert response.status_code == 202, response.text
        deadline = time.monotonic() + 2
        while not captured and time.monotonic() < deadline:
            time.sleep(0.02)
        assert captured, "会诊没有派发到宿主"
        prompt = captured[0]["payload"]["prompt"]
        second_section = prompt.split("\n\n# 第二段", 1)[1].split("\n\n# 第三段", 1)[0]
        assert f"# 第二段{second_section}" == skill


def test_inverted_geometry_abstains(client: TestClient) -> None:
    snapshot_id = _create(client)
    captured: list[dict] = []
    _install_host(client, captured)
    body = _consult(client, snapshot_id).json()
    text = _insight_text(snapshot_id).replace('"invalidation_stop":"59000"', '"invalidation_stop":"61000"')
    response = client.post(
        f"/api/collaboration/consultations/{body['consultation_id']}/result",
        json={"text": text},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["status"] == "ABSTAIN"
    assert record["abstain"]["reason_code"] == "GEOMETRY_INVERTED"


def test_conflicting_evidence_abstains(client: TestClient) -> None:
    snapshot_id = _create(client)
    captured: list[dict] = []
    _install_host(client, captured)
    body = _consult(client, snapshot_id).json()
    text = _insight_text(snapshot_id).replace(
        f"snapshot:{snapshot_id}", "keylevel:not-in-catalog"
    )
    response = client.post(
        f"/api/collaboration/consultations/{body['consultation_id']}/result",
        json={"text": text},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["status"] == "ABSTAIN"
    assert record["abstain"]["reason_code"] == "EVIDENCE_CONFLICT"


def test_package_contains_bounded_fields(client: TestClient) -> None:
    snapshot_id = _create(client)
    captured: list[dict] = []
    _install_host(client, captured)
    body = _consult(client, snapshot_id, drawing_ids=[]).json()
    record = client.get(
        f"/api/collaboration/consultations/{body['consultation_id']}"
    ).json()
    package = record["package"]
    assert package["snapshot_id"] == snapshot_id
    assert package["instrument_id"] == INSTRUMENT
    assert package["timeframe"] == "1m"
    assert package["catalysts"] == []
    assert package["drawings"] == []
    assert 0 < len(package["evidence_catalog"]) <= 40
    kinds = {item["kind"] for item in package["evidence_catalog"]}
    assert {"SNAPSHOT", "CHART_VIEW", "KEY_LEVEL"} <= kinds


def test_usage_report_is_idempotent_and_missing_not_counted(client: TestClient) -> None:
    report = {
        "dsh_session_id": "dsh-session-a",
        "dsh_turn_id": "3",
        "input_tokens": 120,
        "cached_input_tokens": 40,
        "output_tokens": 30,
        "source": "host",
    }
    first = client.post("/api/collaboration/usage", json=report, headers=AUTH)
    assert first.status_code == 200
    assert first.json()["recorded"] is True
    assert first.json()["duplicate"] is False

    second = client.post("/api/collaboration/usage", json=report, headers=AUTH)
    assert second.status_code == 200
    assert second.json()["recorded"] is False
    assert second.json()["duplicate"] is True

    missing = client.post(
        "/api/collaboration/usage",
        json={
            "dsh_session_id": "dsh-session-a",
            "dsh_turn_id": "4",
            "source": "missing",
        },
        headers=AUTH,
    )
    assert missing.status_code == 200

    # 先报 missing、后由轨迹补扫找到真实用量时允许补全；完整轮次仍不可覆盖。
    backfilled = client.post(
        "/api/collaboration/usage",
        json={
            "dsh_session_id": "dsh-session-a",
            "dsh_turn_id": "4",
            "input_tokens": 50,
            "cached_input_tokens": 10,
            "output_tokens": 5,
            "source": "scan",
        },
        headers=AUTH,
    )
    assert backfilled.status_code == 200
    assert backfilled.json()["recorded"] is True
    assert backfilled.json()["duplicate"] is False

    overwrite = client.post(
        "/api/collaboration/usage",
        json={**report, "input_tokens": 999, "source": "scan"},
        headers=AUTH,
    )
    assert overwrite.status_code == 200
    assert overwrite.json()["recorded"] is False

    usage = client.get("/api/collaboration/usage").json()
    total = usage["summary"]
    assert total["input_tokens"] == 170
    assert total["cached_input_tokens"] == 50
    assert total["output_tokens"] == 35
    assert total["turns"] == 2
    assert total["missing_reports"] == 0
    assert len(usage["recent"]) == 2


def test_usage_requires_auth_and_rejects_all_zero_silence(client: TestClient) -> None:
    assert client.post("/api/collaboration/usage", json={}).status_code == 401
    response = client.post(
        "/api/collaboration/usage",
        json={"dsh_session_id": "s", "dsh_turn_id": "1"},
        headers=AUTH,
    )
    assert response.status_code == 422


def test_host_dispatch_ignores_ambient_http_proxy(client, monkeypatch) -> None:
    """宿主派发是回环调用，不能被环境里的 HTTP 代理劫持。"""
    import asyncio
    import http.server
    import threading

    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
            length = int(self.headers.get("content-length", "0"))
            self.rfile.read(length)
            seen.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    try:
        service = client.app.state.runtime.collaborations
        asyncio.run(
            service._post_to_host(
                f"http://127.0.0.1:{server.server_port}",
                {"consultation_id": "cons_proxy_check"},
            )
        )
        assert seen == ["/talaria/consult"]
    finally:
        server.shutdown()
        thread.join(timeout=5)
