"""协同会诊 API：快通道冻结、会诊包、Abstain、宿主回调与 Token 幂等。"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
    service = client.app.state.collaboration_service
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
    service = client.app.state.collaboration_service
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
        service = client.app.state.collaboration_service
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
