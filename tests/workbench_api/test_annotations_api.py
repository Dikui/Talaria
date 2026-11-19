"""智能体标注 API：硬门控、12 条上限、采纳/忽略/清理与草案预警闭环。"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import ROUND_DOWN, Decimal
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
TICK = Decimal("0.1")


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
    (dist / "index.html").write_text(
        "<!doctype html><title>Talaria</title>", encoding="utf-8"
    )
    return dist


@pytest.fixture
def client(tmp_path: Path, fixture_dir: Path, dist_dir: Path) -> Iterator[TestClient]:
    app = create_app(_settings(tmp_path, fixture_dir, dist=dist_dir))
    with TestClient(app) as test_client:
        yield test_client


def _snapshot(client: TestClient, instrument_id: str = INSTRUMENT) -> dict:
    response = client.post(
        "/api/snapshots",
        json={"instrument_id": instrument_id, "timeframes": ["1m"]},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    snapshot_id = response.json()["snapshot_id"]
    bars = client.get(
        f"/api/snapshots/{snapshot_id}/bars",
        params={"timeframe": "1m", "limit": 1_000},
        headers=AUTH,
    )
    assert bars.status_code == 200, bars.text
    levels = client.get(
        f"/api/snapshots/{snapshot_id}/key-levels",
        params={"timeframe": "1m"},
        headers=AUTH,
    )
    assert levels.status_code == 200, levels.text
    payload = levels.json()
    return {
        "snapshot_id": snapshot_id,
        "bars": bars.json()["bars"],
        "level_id": payload["levels"][0]["level_id"] if payload["levels"] else None,
    }


def _price(close: str) -> str:
    return format(Decimal(close).quantize(TICK, rounding=ROUND_DOWN), "f")


def _body(
    snapshot: dict,
    *,
    kind: str = "LEVEL",
    price_low: str | None = None,
    price_high: str | None = None,
    evidence: list[dict] | None = None,
    ts_anchor: int | None = None,
    round: int = 1,
    session_id: str = "session-a",
    instrument_id: str = INSTRUMENT,
    snapshot_id: str | None = None,
) -> dict:
    last = snapshot["bars"][-1]
    price = _price(last["close"]) if price_low is None else price_low
    high = price if price_high is None else price_high
    return {
        "snapshot_id": snapshot_id or snapshot["snapshot_id"],
        "instrument_id": instrument_id,
        "timeframe": "1m",
        "session_id": session_id,
        "round": round,
        "kind": kind,
        "label": "智能体标注",
        "rationale": "前低与快照关键位重叠",
        "price_low": price,
        "price_high": high,
        "ts_anchor": ts_anchor if ts_anchor is not None else last["timestamp"],
        "evidence_refs": (
            evidence
            if evidence is not None
            else [{"kline_ts": last["timestamp"], "note": "最近一根确认 K 线"}]
        ),
    }


def _create(client: TestClient, body: dict):
    return client.post("/api/annotations", json=body, headers=AUTH)


def _annotation_body(client: TestClient) -> tuple[dict, dict]:
    snapshot = _snapshot(client)
    return snapshot, _body(snapshot)


def _code(response) -> str:
    assert response.status_code == 422, response.text
    return response.json()["code"]


def test_create_requires_bearer(client: TestClient) -> None:
    snapshot = _snapshot(client)
    response = client.post("/api/annotations", json=_body(snapshot))
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_unknown_snapshot_rejected(client: TestClient) -> None:
    snapshot = _snapshot(client)
    response = _create(client, _body(snapshot, snapshot_id="snap_missing"))
    assert _code(response) == "SNAPSHOT_MISMATCH"


def test_instrument_mismatch_rejected(client: TestClient) -> None:
    snapshot = _snapshot(client)
    response = _create(client, _body(snapshot, instrument_id="ETH-USDT-PERP"))
    assert _code(response) == "SNAPSHOT_MISMATCH"


def test_empty_evidence_rejected(client: TestClient) -> None:
    snapshot = _snapshot(client)
    response = _create(client, _body(snapshot, evidence=[]))
    assert _code(response) == "EVIDENCE_EMPTY"


def test_unresolved_evidence_rejected(client: TestClient) -> None:
    snapshot = _snapshot(client)
    response = _create(
        client,
        _body(snapshot, evidence=[{"level_id": "kl_missing"}]),
    )
    assert _code(response) == "EVIDENCE_UNRESOLVED"


def test_level_evidence_is_accepted(client: TestClient) -> None:
    snapshot = _snapshot(client)
    assert snapshot["level_id"] is not None
    response = _create(
        client,
        _body(snapshot, evidence=[{"level_id": snapshot["level_id"]}]),
    )
    assert response.status_code == 201, response.text
    assert response.json()["evidence_refs"][0]["level_id"] == snapshot["level_id"]


def test_non_positive_price_rejected(client: TestClient) -> None:
    snapshot = _snapshot(client)
    response = _create(client, _body(snapshot, price_low="0", price_high="0"))
    assert _code(response) == "PRICE_INVALID"


def test_tick_misaligned_price_rejected(client: TestClient) -> None:
    snapshot = _snapshot(client)
    last = snapshot["bars"][-1]
    misaligned = Decimal(_price(last["close"])) + Decimal("0.05")
    response = _create(
        client,
        _body(snapshot, price_low=format(misaligned, "f")),
    )
    assert _code(response) == "PRICE_NOT_TICK_ALIGNED"


def test_out_of_bounds_price_rejected(client: TestClient) -> None:
    snapshot = _snapshot(client)
    last = snapshot["bars"][-1]
    far = Decimal(last["high"]) + Decimal("100000")
    response = _create(
        client,
        _body(snapshot, price_low=format(far.quantize(TICK), "f")),
    )
    assert _code(response) == "PRICE_OUT_OF_BOUNDS"


def test_time_before_snapshot_rejected(client: TestClient) -> None:
    snapshot = _snapshot(client)
    start = snapshot["bars"][0]["timestamp"]
    response = _create(client, _body(snapshot, ts_anchor=start - 120_000))
    assert _code(response) == "TIME_BEFORE_SNAPSHOT"


def test_time_too_far_future_rejected(client: TestClient) -> None:
    snapshot = _snapshot(client)
    last = snapshot["bars"][-1]["timestamp"]
    response = _create(
        client,
        _body(snapshot, ts_anchor=last + 101 * 60_000),
    )
    assert _code(response) == "TIME_TOO_FAR_FUTURE"


def test_create_persists_and_lists_with_filters(client: TestClient) -> None:
    snapshot, body = _annotation_body(client)
    response = _create(client, body)
    assert response.status_code == 201, response.text
    annotation = response.json()
    assert annotation["status"] == "ACTIVE"
    assert annotation["snapshot_id"] == snapshot["snapshot_id"]

    listed = client.get(
        "/api/annotations",
        params={
            "instrument_id": INSTRUMENT,
            "snapshot_id": snapshot["snapshot_id"],
            "session_id": "session-a",
        },
        headers=AUTH,
    )
    assert listed.status_code == 200
    assert [item["annotation_id"] for item in listed.json()["annotations"]] == [
        annotation["annotation_id"]
    ]

    sessions = client.get("/api/annotations/sessions", headers=AUTH)
    assert sessions.status_code == 200
    assert sessions.json()["sessions"][0]["session_id"] == "session-a"
    assert sessions.json()["sessions"][0]["annotation_count"] == 1


def test_thirteenth_annotation_archives_oldest_round(client: TestClient) -> None:
    snapshot = _snapshot(client)
    for round_no in range(12):
        response = _create(
            client,
            _body(snapshot, round=round_no, session_id="session-cap"),
        )
        assert response.status_code == 201, response.text

    thirteenth = _create(
        client,
        _body(snapshot, round=12, session_id="session-cap"),
    )
    assert thirteenth.status_code == 201, thirteenth.text
    assert thirteenth.json()["status"] == "ACTIVE"

    active = client.get(
        "/api/annotations",
        params={"session_id": "session-cap"},
        headers=AUTH,
    )
    assert len(active.json()["annotations"]) == 12
    assert all(item["status"] == "ACTIVE" for item in active.json()["annotations"])

    audit = client.get(
        "/api/annotations",
        params={"session_id": "session-cap", "include_archived": "true"},
        headers=AUTH,
    )
    archived = [
        item for item in audit.json()["annotations"] if item["status"] == "ARCHIVED"
    ]
    assert len(archived) == 1
    assert archived[0]["round"] == 0
    assert "12" in archived[0]["archived_reason"]


def test_adopt_copies_to_independent_human_drawing(client: TestClient) -> None:
    snapshot, body = _annotation_body(client)
    created = _create(client, body).json()
    adopted = client.post(
        f"/api/annotations/{created['annotation_id']}/adopt",
        headers=AUTH,
    )
    assert adopted.status_code == 200, adopted.text
    payload = adopted.json()
    assert payload["annotation"]["status"] == "ADOPTED"
    drawing_id = payload["adoption"]["drawing_id"]
    assert payload["annotation"]["adopted_drawing_id"] == drawing_id

    drawing = client.get(f"/api/drawings/{drawing_id}", headers=AUTH)
    assert drawing.status_code == 200
    assert drawing.json()["kind"] == "HORIZONTAL_LINE"
    assert drawing.json()["instrument_id"] == INSTRUMENT

    again = client.post(
        f"/api/annotations/{created['annotation_id']}/adopt",
        headers=AUTH,
    )
    assert again.status_code == 200
    assert again.json()["adoption"]["drawing_id"] == drawing_id


def test_dismiss_and_clear_session(client: TestClient) -> None:
    snapshot = _snapshot(client)
    first = _create(client, _body(snapshot, session_id="session-dismiss")).json()
    second = _create(
        client, _body(snapshot, kind="ZONE", price_high=None, session_id="session-dismiss")
    ).json()

    dismissed = client.post(
        f"/api/annotations/{first['annotation_id']}/dismiss",
        headers=AUTH,
    )
    assert dismissed.status_code == 200
    assert dismissed.json()["status"] == "DISMISSED"

    cleared = client.post(
        "/api/annotations/clear",
        json={"instrument_id": INSTRUMENT, "session_id": "session-dismiss"},
        headers=AUTH,
    )
    assert cleared.status_code == 200
    assert cleared.json()["cleared"] == 1

    listed = client.get(
        "/api/annotations",
        params={"session_id": "session-dismiss"},
        headers=AUTH,
    )
    assert {item["status"] for item in listed.json()["annotations"]} == {"DISMISSED"}
    assert second["annotation_id"]


def test_agent_alert_is_draft_until_confirmed_and_dismiss_never_triggers(
    client: TestClient,
) -> None:
    condition = {
        "kind": "METRIC_THRESHOLD",
        "metric": "last_price",
        "direction": "ABOVE",
        "threshold": "100000",
    }
    first = client.post(
        "/api/alerts",
        json={
            "instrument_id": INSTRUMENT,
            "timeframe": "1m",
            "condition": condition,
            "note": "智能体草案",
            "draft": True,
            "repeat": False,
        },
        headers=AUTH,
    )
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "DRAFT"

    confirmed = client.post(
        f"/api/alerts/{first.json()['alert_id']}/confirm",
        headers=AUTH,
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "ARMED"

    second = client.post(
        "/api/alerts",
        json={
            "instrument_id": INSTRUMENT,
            "timeframe": "1m",
            "condition": condition,
            "note": "将被废弃",
            "draft": True,
        },
        headers=AUTH,
    ).json()
    dismissed = client.post(
        f"/api/alerts/{second['alert_id']}/dismiss",
        headers=AUTH,
    )
    assert dismissed.status_code == 200
    assert dismissed.json()["status"] == "DISABLED"

    triggers = client.get("/api/alerts/triggers", headers=AUTH)
    assert triggers.status_code == 200
    assert triggers.json()["triggers"] == []
