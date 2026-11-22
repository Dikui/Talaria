"""trading/04：用进程内 API 验证草案出站队列与账本单写边界。"""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from talaria.workbench_api.app import create_app
from talaria.workbench_api.settings import AppSettings

pytestmark = pytest.mark.medium
AUTH = {"Authorization": "Bearer trader-session"}
ROOT = "/api/paper"
INSTRUMENT = "BTC-USDT-PERP"


@pytest.fixture
def client(tmp_path, fixture_dir):
    settings = AppSettings(
        data_dir=tmp_path, fixture_dir=fixture_dir, dist_dir=tmp_path,
        token="trader-session", now_ms=lambda: 1_795_022_974_103,
    )
    with TestClient(create_app(settings), headers=AUTH) as client:
        yield client


def test_only_the_current_confirmed_draft_can_reach_the_account(client):
    snapshot = client.post("/api/snapshots", json={"instrument_id": INSTRUMENT, "timeframes": ["1m"]}).json()
    content = {
        "sub_account_id": "sa-one", "instrument_id": INSTRUMENT, "snapshot_id": snapshot["snapshot_id"],
        "direction": "LONG", "entry_low": "100", "entry_high": "101",
        "invalidation_stop": "98", "take_profit": ["104"],
    }
    saved = client.post(f"{ROOT}/drafts", json=content)
    assert saved.status_code == 200, saved.text
    draft = saved.json()
    path = f"{ROOT}/drafts/{draft['draft_id']}"
    deliver = f"{ROOT}/accounts/sa-one/next-draft"
    assert client.post(deliver, json={"instrument_id": INSTRUMENT}).json() is None

    def confirm(value):
        return client.post(f"{path}/confirm", json={
            "version": value["version"], "content_hash": value["content_hash"], "confirmed_by": "交易员",
        })

    assert confirm(draft).status_code == 200
    edited = client.put(path, json={"version": 1, "content": {**content, "entry_high": "102"}}).json()
    assert edited["version"] == 2
    assert confirm(draft).status_code == 409
    assert client.post(deliver, json={"instrument_id": INSTRUMENT}).json() is None
    assert confirm(edited).status_code == 200
    assert client.post(f"{ROOT}/accounts/sa-two/next-draft", json={"instrument_id": INSTRUMENT}).json() is None
    delivery = client.post(deliver, json={"instrument_id": INSTRUMENT}).json()
    assert delivery["version"] == 2 and delivery["content"]["entry_high"] == "102"
    assert delivery["confirmed_by"] == "交易员" and delivery["confirmed_ts"] == 1_795_022_974_103
    assert client.post(deliver, json={"instrument_id": INSTRUMENT}).json() == delivery
    receipt = {"draft_id": draft["draft_id"], "version": 2, "accepted": False, "reason": "DRAFT_STALE"}
    assert client.post(f"{ROOT}/accounts/sa-one/draft-receipt", json=receipt).status_code == 200
    assert client.post(deliver, json={"instrument_id": INSTRUMENT}).json() is None
    assert client.get(f"{ROOT}/drafts").json()[0]["receipt"] == {**receipt, "snapshot_id": None, "plan_id": None}


def test_service_opens_existing_ledger_read_only(client, tmp_path, monkeypatch):
    from talaria.account.contracts import ExecutionEvent
    from talaria.account.ledger import open_ledger

    path = tmp_path / "accounts" / "sa-one" / "ledger.sqlite"
    with open_ledger(path, "setup", ("sa-one",)) as ledger:
        ledger.record([ExecutionEvent(kind="TRADE_STATE", sub_account_id="sa-one", ts_event=100)])
    original = sqlite3.connect
    opened = []

    def connect(database, *args, **kwargs):
        if "ledger.sqlite" in str(database):
            opened.append(database)
            assert kwargs.get("uri") and str(database).endswith("?mode=ro")
        return original(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    response = client.get(f"{ROOT}/accounts")
    assert response.status_code == 200
    assert response.json() == ["sa-one"]
    assert client.get(f"{ROOT}/accounts/sa-one").json()["positions"] == []
    assert client.get(f"{ROOT}/accounts/missing").status_code == 404
    assert len(opened) == 1
    with closing(original(path)) as db:
        assert db.execute("SELECT count(*) FROM execution_events").fetchone() == (1,)


def test_refreeze_binds_new_snapshot_to_cutoff_and_instrument(client):
    cutoff = 1_795_022_974_103
    response = client.post(f"{ROOT}/draft-evidence", json={
        "instrument_id": INSTRUMENT, "timeframes": ["1m"], "cutoff_ts": cutoff,
    })
    assert response.status_code == 200, response.text
    evidence = response.json()
    assert evidence["cutoff_ts"] == cutoff
    assert evidence["instrument_id"] == INSTRUMENT
    assert evidence["bars"]
    assert client.get("/api/snapshots/" + evidence["snapshot_id"]).json()["cutoff_ts"] == cutoff


def test_barrier_projection_includes_only_open_rounds_for_current_instrument(client, tmp_path):
    from talaria.account.contracts import ExecutionEvent, RoundStatus
    from talaria.account.ledger import open_ledger

    account = "sa-one"
    path = tmp_path / "accounts" / account / "ledger.sqlite"

    def position(round_id, instrument, quantity, phase):
        return RoundStatus(round_id=round_id, instrument_id=instrument, sub_account_id=account,
                           direction="LONG", quantity=quantity, stop="98", phase=phase)

    active = position("open", INSTRUMENT, "1", "ACTIVE_PROTECTED")
    closed = position("closed", INSTRUMENT, "0", "CLOSED")
    other = position("other", "ETH-USDT-PERP", "1", "ACTIVE_PROTECTED")
    with open_ledger(path, "setup", (account,)) as ledger:
        ledger.record([
            ExecutionEvent(kind="ENTRY_FILLED", sub_account_id=account, ts_event=100, position=active,
                           take_profit=("110", "120"), quantity="1", price="103"),
            ExecutionEvent(kind="EXIT_FILLED", sub_account_id=account, ts_event=101, position=closed,
                           take_profit=("110",), quantity="1", price="110"),
            ExecutionEvent(kind="ENTRY_FILLED", sub_account_id=account, ts_event=102, position=other,
                           take_profit=("110",), quantity="1", price="103"),
        ])
    view = client.get(f"{ROOT}/accounts/{account}").json()
    assert [row["round_id"] for row in view["rounds"]] == ["other", "closed", "open"]
    assert [row["kind"] for row in view["fills"]] == ["ENTRY_FILLED", "EXIT_FILLED", "ENTRY_FILLED"]
    barriers = client.get(f"{ROOT}/barriers", params={"instrument": INSTRUMENT}).json()
    assert [(row["round_id"], row["stop"], row["take_profit"]) for row in barriers] == [
        ("open", "98", ["110", "120"]),
    ]


def test_claim_follows_confirmation_order_with_equal_timestamps(client):
    snap = client.post("/api/snapshots", json={"instrument_id": INSTRUMENT, "timeframes": ["1m"]}).json()
    content = dict(sub_account_id="sa-one", instrument_id=INSTRUMENT, snapshot_id=snap["snapshot_id"],
                   direction="LONG", entry_low="100", entry_high="101", invalidation_stop="98", take_profit=["104"])
    drafts = [client.post(f"{ROOT}/drafts", json=content).json() for _ in range(2)]
    for draft in reversed(drafts):
        assert client.post(f"{ROOT}/drafts/{draft['draft_id']}/confirm", json={
            "version": 1, "content_hash": draft["content_hash"], "confirmed_by": "trader",
        }).status_code == 200
    claimed = client.post(f"{ROOT}/accounts/sa-one/next-draft", json={"instrument_id": INSTRUMENT}).json()
    assert claimed["draft_id"] == drafts[1]["draft_id"]


def test_confirmation_retry_after_database_interrupt_keeps_submission_order(tmp_path):
    from talaria.foundation.paper import DraftConfirmation, DraftContent
    from talaria.workbench_api.state_repositories.paper import DraftRepository, SCHEMA
    with closing(sqlite3.connect(tmp_path / "state", isolation_level=None)) as db:
        db.executescript(SCHEMA)
        repo = DraftRepository(db, lambda: 1000)
        content = DraftContent(sub_account_id="alpha", instrument_id=INSTRUMENT, snapshot_id="basis",
                               direction="LONG", entry_low="100", entry_high="101", invalidation_stop="90", take_profit=["120"])
        first, second = (repo.save(content, 1000) for _ in range(2))
        confirmation = DraftConfirmation(version=1, content_hash=first.content_hash, confirmed_by="trader")
        db.execute("""CREATE TRIGGER interrupt BEFORE UPDATE OF confirmation ON paper_drafts
                      BEGIN SELECT RAISE(ABORT, 'interrupted'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="interrupted"):
            repo.confirm(first.draft_id, confirmation)
        db.execute("DROP TRIGGER interrupt")
        repo.confirm(second.draft_id, confirmation)
        repo.confirm(first.draft_id, confirmation)
        assert repo.claim("alpha", INSTRUMENT).draft_id == second.draft_id
