"""预警 API：CRUD、草案确认、结构化错误与 Bearer 写保护。"""

from __future__ import annotations

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


def _settings(tmp_path: Path, dist: Path, fixture_dir: Path) -> AppSettings:
    return AppSettings(
        data_dir=tmp_path / "data",
        dist_dir=dist,
        fixture_dir=fixture_dir,
        token=TOKEN,
        now_ms=lambda: NOW_MS,
    )


@pytest.fixture
def dist_dir(tmp_path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<!doctype html><title>Talaria</title>", encoding="utf-8"
    )
    return dist


@pytest.fixture
def app(tmp_path, fixture_dir, dist_dir):
    return create_app(_settings(tmp_path, dist_dir, fixture_dir))


@pytest.fixture
def client(app):
    with TestClient(app) as instance:
        yield instance


def _metric_rule(value: str) -> dict:
    return {
        "version": "1", "timing": "event", "expression": {
            "op": "compare", "test": "gte",
            "left": {"source": "event", "metric": "last_price"},
            "right": {"source": "constant", "value": value},
        },
    }


def _metric_body(**overrides) -> dict:
    body = {
        "instrument_id": INSTRUMENT,
        "timeframe": "1m",
        "condition": _metric_rule("105.10"),
        "note": "突破前高",
        "cooldown_ms": 60_000,
        "repeat": True,
    }
    body.update(overrides)
    return body


def _create(client: TestClient, body: dict | None = None, **overrides) -> dict:
    response = client.post("/api/alerts", json=body or _metric_body(**overrides), headers=AUTH)
    assert response.status_code == 201, response.text
    return response.json()


def _drawing(
    client: TestClient, kind: str, points: list[dict], *, text: str = ""
) -> dict:
    response = client.post(
        "/api/drawings",
        json={
            "instrument_id": INSTRUMENT,
            "kind": kind,
            "visible_timeframes": ["1m"],
            "points": points,
            "text": text,
        },
        headers=AUTH,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_create_list_patch_and_delete_round_trip(client) -> None:
    created = _create(client)
    assert created["alert_id"].startswith("alr_")
    assert created["status"] == "ARMED"
    assert created["condition"]["timing"] == "event"
    assert created["condition"]["expression"]["right"]["value"] == "105.10"
    assert isinstance(created["condition"]["expression"]["right"]["value"], str)
    assert created["note"] == "突破前高"
    assert created["trigger_count"] == 0

    listed = client.get("/api/alerts", params={"status": "ARMED"})
    assert listed.status_code == 200
    assert [item["alert_id"] for item in listed.json()["alerts"]] == [created["alert_id"]]

    patched = client.patch(
        f"/api/alerts/{created['alert_id']}",
        json={"condition": _metric_rule("99.9")},
        headers=AUTH,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["condition"]["expression"]["right"]["value"] == "99.9"
    assert patched.json()["alert_id"] == created["alert_id"]

    triggers = client.get(
        "/api/alerts/triggers", params={"alert_id": created["alert_id"]}
    )
    assert triggers.status_code == 200
    assert triggers.json() == {"triggers": []}

    deleted = client.delete(f"/api/alerts/{created['alert_id']}", headers=AUTH)
    assert deleted.status_code == 200
    assert client.get("/api/alerts").json() == {"alerts": []}
    assert client.get(f"/api/alerts/{created['alert_id']}").status_code == 404


def test_draft_requires_confirmation_and_dismiss_disables(client) -> None:
    draft = _create(client, draft=True)
    assert draft["status"] == "DRAFT"

    confirmed = client.post(f"/api/alerts/{draft['alert_id']}/confirm", headers=AUTH)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "ARMED"

    again = client.post(f"/api/alerts/{draft['alert_id']}/confirm", headers=AUTH)
    assert again.status_code == 200
    assert again.json()["status"] == "ARMED"

    dismissed = client.post(f"/api/alerts/{draft['alert_id']}/dismiss", headers=AUTH)
    assert dismissed.status_code == 200
    assert dismissed.json()["status"] == "DISABLED"

    confirm_disabled = client.post(
        f"/api/alerts/{draft['alert_id']}/confirm", headers=AUTH
    )
    assert confirm_disabled.status_code == 422
    assert confirm_disabled.json()["code"] == "VALIDATION_ERROR"


def test_overlay_condition_requires_existing_non_text_drawing(client) -> None:
    horizontal = _drawing(client, "HORIZONTAL_LINE", [{"ts": NOW_MS, "price": "100.5"}])
    overlay = {
        "version": "1", "timing": "bar_close",
        "expression": {"op": "overlay", "direction": "ABOVE", "drawing_id": horizontal['drawing_id'], "mode": "CLOSE_BREAK"},
    }
    created = _create(client, condition=overlay, note="突破水平线")
    assert created["condition"]["expression"]["drawing_id"] == horizontal["drawing_id"]

    text = _drawing(
        client,
        "TEXT_LABEL",
        [{"ts": NOW_MS, "price": "100.5"}],
        text="观察位",
    )
    rejected = client.post(
        "/api/alerts",
        json=_metric_body(
            condition={
                "version": "1", "timing": "event",
                "expression": {"op": "overlay", "direction": "ABOVE", "drawing_id": text['drawing_id'], "mode": "TOUCH"},
            }
        ),
        headers=AUTH,
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "VALIDATION_ERROR"

    missing = client.post(
        "/api/alerts",
        json=_metric_body(
            condition={
                "version": "1", "timing": "event",
                "expression": {"op": "overlay", "direction": "ABOVE", "drawing_id": 'drw_missing', "mode": "TOUCH"},
            }
        ),
        headers=AUTH,
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "DRAWING_NOT_FOUND"


def test_indicator_signal_is_validated_server_side(client) -> None:
    indicator = {
        "version": "1", "timing": "bar_close",
        "expression": {
            "op": "compare", "test": "lte",
            "left": {"source": "indicator", "name": 'RSI', "params": {"period": 14}},
            "right": {"source": "constant", "value": '30'},
        },
    }
    created = _create(client, condition=indicator)
    assert created["condition"]["expression"]["left"]["name"] == "RSI"

    derived = client.post(
        "/api/alerts",
        json=_metric_body(
            condition={
                "version": "1", "timing": "bar_close",
                "expression": {
                    "op": "compare", "test": "lte",
                    "left": {"source": "indicator", "name": 'FUNDING_QUANTILE'},
                    "right": {"source": "constant", "value": '0.1'},
                },
            }
        ),
        headers=AUTH,
    )
    assert derived.status_code == 201
    assert derived.json()["condition"]["expression"]["left"]["name"] == "FUNDING_QUANTILE"

    bad_channel = client.post(
        "/api/alerts",
        json=_metric_body(
            condition={
                "version": "1", "timing": "bar_close",
                "expression": {
                    "op": "compare", "test": "lte",
                    "left": {"source": "indicator", "name": "EMA", "channel": 'upper', "params": {"period": 20}},
                    "right": {"source": "constant", "value": '1'},
                },
            }
        ),
        headers=AUTH,
    )
    assert bad_channel.status_code == 422


def test_indicator_signal_persists_explicit_period_and_vwap_anchor(client) -> None:
    ema = _create(
        client,
        condition={
            "version": "1", "timing": "bar_close",
            "expression": {
                "op": "compare", "test": "lte",
                "left": {"source": "indicator", "name": "EMA"},
                "right": {"source": "constant", "value": '1'},
            },
        },
    )
    assert ema["condition"]["expression"]["left"]["params"]["period"] == 9
    assert ema["condition"]["expression"]["left"]["params"]["anchor"] is None

    vwap = _create(
        client,
        condition={
            "version": "1", "timing": "bar_close",
            "expression": {
                "op": "compare", "test": "lte",
                "left": {"source": "indicator", "name": "VWAP"},
                "right": {"source": "constant", "value": '1'},
            },
        },
    )
    assert vwap["condition"]["expression"]["left"]["params"]["period"] == 1
    assert vwap["condition"]["expression"]["left"]["params"]["anchor"] == "session"

    snapshot = _create(
        client,
        condition={
            "version": "1", "timing": "bar_close",
            "expression": {
                "op": "compare", "test": "lte",
                "left": {"source": "indicator", "name": "VWAP", "params": {"anchor": "snapshot"}},
                "right": {"source": "constant", "value": '1'},
            },
        },
    )
    assert snapshot["condition"]["expression"]["left"]["params"]["anchor"] == "snapshot"

    bad_anchor = client.post(
        "/api/alerts",
        json=_metric_body(
            condition={
                "version": "1", "timing": "bar_close",
                "expression": {
                    "op": "compare", "test": "lte",
                    "left": {"source": "indicator", "name": "EMA", "params": {"anchor": "session"}},
                    "right": {"source": "constant", "value": '1'},
                },
            }
        ),
        headers=AUTH,
    )
    assert bad_anchor.status_code == 422


def test_write_endpoints_require_bearer_and_reads_do_not(client) -> None:
    created = _create(client)
    alert_id = created["alert_id"]

    for method, path, body in (
        ("post", "/api/alerts", _metric_body()),
        ("patch", f"/api/alerts/{alert_id}", {"note": "x"}),
        ("delete", f"/api/alerts/{alert_id}", None),
        ("post", f"/api/alerts/{alert_id}/confirm", None),
        ("post", f"/api/alerts/{alert_id}/dismiss", None),
    ):
        response = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
        assert response.status_code == 401, (method, path, response.text)

    assert client.get("/api/alerts").status_code == 200
    assert client.get("/api/alerts/triggers").status_code == 200
    assert client.get(f"/api/alerts/{alert_id}").status_code == 200


def test_unknown_instrument_and_timeframe_are_structured(client) -> None:
    unknown = client.post(
        "/api/alerts",
        json=_metric_body(instrument_id="NOPE-USDT-PERP"),
        headers=AUTH,
    )
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "INSTRUMENT_NOT_FOUND"

    bad_timeframe = client.post(
        "/api/alerts", json=_metric_body(timeframe="7m"), headers=AUTH
    )
    assert bad_timeframe.status_code == 422
    assert bad_timeframe.json()["code"] == "VALIDATION_ERROR"

    unknown_alert = client.get("/api/alerts/alr_missing")
    assert unknown_alert.status_code == 404
    assert unknown_alert.json()["code"] == "ALERT_NOT_FOUND"


def test_long_note_and_single_shot_expiry_fields_are_preserved(client) -> None:
    note = "长备注" * 120
    created = _create(
        client,
        note=note,
        repeat=False,
        cooldown_ms=0,
        expires_at=NOW_MS + 3_600_000,
    )
    assert created["note"] == note
    assert created["repeat"] is False
    assert created["cooldown_ms"] == 0
    assert created["expires_at"] == NOW_MS + 3_600_000

    too_long = client.post(
        "/api/alerts", json=_metric_body(note="x" * 501), headers=AUTH
    )
    assert too_long.status_code == 422
