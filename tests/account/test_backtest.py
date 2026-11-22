"""trading/01 的 CLI、Parquet 回放和账本端到端契约。"""

import shutil
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest

from talaria.cli.main import main
from talaria.foundation.connector import RawBar
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.perception.cold_store import ColdBatch, ColdStore

pytestmark = [pytest.mark.medium, pytest.mark.spec]
START = 1_793_491_200_000
MINUTE = 60_000
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def replay(tmp_path):
    scheme_dir = tmp_path / "scheme"
    shutil.copytree(FIXTURES, scheme_dir, ignore=shutil.ignore_patterns("__pycache__"))
    data_dir = tmp_path / "data"
    instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")

    def bar(ts, price, *, revision=0, delay=0):
        value = Decimal(price)
        available = ts + MINUTE + delay
        return RawBar(
            symbol=instrument.symbol, timeframe="1m", ts_event=ts,
            open=value, high=value, low=value, close=value, volume=Decimal(1),
            turnover=value, ts_available=available, ts_ingested=available,
            ts_committed=available, revision=revision,
        )

    with closing(ColdStore(data_dir)) as store:
        store.publish(ColdBatch(instrument, "1m", [
            bar(START, "11"),
            bar(START, "99", revision=1, delay=MINUTE + 1),
            bar(START + MINUTE, "12", delay=7_000),
            bar(START + 2 * MINUTE, "13"),
        ]))
    return scheme_dir / "scheme.yaml", data_dir


def launch(scheme, data_dir):
    return main(["account", "run", str(scheme), "--mode", "backtest", "--data-dir", str(data_dir)])


def test_cli_replays_same_scheme_data_and_seed_into_equal_journals(replay, capsys):
    scheme, data_dir = replay
    assert launch(scheme, data_dir) == 0
    assert launch(scheme, data_dir) == 0

    from talaria.account.ledger import read_journal

    runs = sorted((data_dir / "runs").iterdir())
    assert len(runs) == 2
    output = capsys.readouterr().out
    assert all(str(run) in output for run in runs)
    assert not (data_dir / "accounts").exists()
    first, second = [read_journal(run / "ledger.sqlite") for run in runs]
    assert first == second
    assert [(event.kind, event.ts_event, event.quantity) for event in first.events] == [
        ("ENTRY_FILLED", START + 2 * MINUTE + 7_000, Decimal(2)),
    ]
    assert len(first.entries) == 1
    entry = first.entries[0]
    assert entry.sub_account_id == "example"
    assert [(posting.account, posting.currency, posting.amount) for posting in entry.postings] == [
        ("POSITION_COST", "USDT", Decimal("25.32")),
        ("TRADE_PAYABLE", "USDT", Decimal("-25.32")),
    ]


def test_strategy_quantity_is_rejected_and_audited(replay):
    from talaria.account.ledger import read_journal

    scheme, data_dir = replay
    scheme.write_text(scheme.read_text().replace('trigger_price: "11"', 'trigger_price: "11", max_position: "100"'))
    assert launch(scheme, data_dir) == 0

    path, = (data_dir / "runs").glob("*/ledger.sqlite")
    journal = read_journal(path)
    assert [(event.kind, event.reason) for event in journal.events] == [
        ("CANDIDATE_REJECTED", "STRATEGY_QUANTITY_NOT_ALLOWED"),
    ]
    assert journal.entries == ()


@pytest.mark.parametrize(("slot", "params", "message"), [
    ("risk", "resize: true", "定仓"),
    ("fill", "unapproved: true", "未经风控批准"),
])
def test_unapproved_openings_cannot_enter_the_ledger(replay, capsys, slot, params, message):
    from talaria.account.ledger import read_journal

    scheme, data_dir = replay
    scheme.write_text(scheme.read_text().replace(
        f"{slot}: replay_plugins.py:{slot}",
        f"{slot}: {{plugin: replay_plugins.py:{slot}, params: {{{params}}}}}",
    ))
    assert launch(scheme, data_dir) == 2
    assert message in capsys.readouterr().err
    path, = (data_dir / "runs").glob("*/ledger.sqlite")
    assert read_journal(path).events == ()
    assert read_journal(path).entries == ()


def test_a_risk_rejection_is_audited_without_a_fill(replay):
    from talaria.account.ledger import read_journal

    scheme, data_dir = replay
    scheme.write_text(scheme.read_text().replace(
        "risk: replay_plugins.py:risk", "risk: {plugin: replay_plugins.py:risk, params: {deny: true}}",
    ))
    assert launch(scheme, data_dir) == 0
    path, = (data_dir / "runs").glob("*/ledger.sqlite")
    journal = read_journal(path)
    assert [(event.kind, event.reason) for event in journal.events] == [
        ("CANDIDATE_REJECTED", "TEST_RISK_LIMIT"),
    ]
    assert journal.entries == ()
