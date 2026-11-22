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
    assert [(event.kind, event.ts_event, event.quantity, event.price) for event in first.events] == [
        ("ENTRY_FILLED", START + 2 * MINUTE + 7_000, Decimal(2), Decimal("12.66")),
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
    journal = read_journal(path)
    if slot == "fill":
        assert [(event.kind, event.plan_id) for event in journal.events] == [
            ("EXECUTION_REJECTED", "unapproved"),
        ]
    else:
        audit, = journal.events
        assert audit.kind == "CANDIDATE_REJECTED" and message in audit.reason
    assert journal.entries == ()


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


def test_mutating_a_strategy_bar_cannot_change_the_fill_market(replay):
    from talaria.account.ledger import read_journal

    scheme, data_dir = replay
    strategy = scheme.parent / "replay_strategy.py"
    strategy.write_text(strategy.read_text().replace(
        "        if self.triggered or bar.close",
        """        if self.triggered:
            try:
                bar.close = Decimal("999")
            except ValueError:
                pass
        if self.triggered or bar.close""",
    ))
    assert launch(scheme, data_dir) == 0
    path, = (data_dir / "runs").glob("*/ledger.sqlite")
    fill, = read_journal(path).events
    assert fill.price == Decimal("12.66")


def test_a_fill_at_signal_bar_close_is_rejected(replay, capsys):
    from talaria.account.ledger import read_journal

    scheme, data_dir = replay
    fill_plugin = scheme.parent / "replay_plugins.py"
    fill_plugin.write_text(fill_plugin.read_text().replace(
        "ts_event=event.as_of", "ts_event=plan.candidate.ts_decision",
    ))
    assert launch(scheme, data_dir) == 2
    assert "晚于信号柱收盘" in capsys.readouterr().err
    path, = (data_dir / "runs").glob("*/ledger.sqlite")
    journal = read_journal(path)
    assert [event.kind for event in journal.events] == ["EXECUTION_REJECTED"]
    assert journal.entries == ()


def test_host_truncates_strategy_ctx_even_when_data_plugin_overrides_bars(replay):
    from talaria.account.ledger import read_journal

    scheme, data_dir = replay
    source = scheme.parent / "unfiltered_data.py"
    source.write_text("""\
from dataclasses import replace
from talaria.account.plugins.parquet import ParquetReplay, ReplayParams
from talaria.account.scheme import Slot, SlotPlugin
from talaria.components import Ctx

class Unfiltered(Ctx):
    def bars(self, timeframe=None):
        bars = super().bars(timeframe)
        if len(bars) != 2:
            return bars
        future_revision = bars[0].model_copy(update={
            "close": 99, "ts_available": self.now + 1, "revision": 9,
        })
        return (future_revision, *bars[1:])

class Data(ParquetReplay):
    def events(self):
        for event in super().events():
            def unfiltered(*args, **kwargs):
                rows = event.source.read_bars(*args, **kwargs)
                if len(rows) < 2:
                    return rows
                future = rows[0].model_copy(update={
                    "close": 99, "ts_available": kwargs["as_of"] + 1, "revision": 9,
                })
                return (*rows, future)

            ctx = Unfiltered(
                event.instrument_id, event.timeframe, event.source.history_start, event.as_of,
                unfiltered, _bar_ts=event.bar.ts_event,
            )
            yield replace(event, ctx=ctx, source=None)

plugin = SlotPlugin(Slot.DATA, (1,), Data, ReplayParams)
""")
    scheme.write_text(scheme.read_text().replace("parquet_replay", "unfiltered_data.py:plugin"))
    strategy = scheme.parent / "replay_strategy.py"
    strategy.write_text(strategy.read_text().replace(
        "        bar = ctx.bars()[-1]", """        bars = ctx.bars()
        assert all(bar.ts_available <= ctx.now for bar in bars)
        if len(bars) == 2:
            assert bars[0].close == Decimal(99)
        bar = bars[-1]""",
    ))
    assert launch(scheme, data_dir) == 0
    path, = (data_dir / "runs").glob("*/ledger.sqlite")
    assert [event.kind for event in read_journal(path).events] == ["ENTRY_FILLED"]


@pytest.mark.parametrize("reverse_batch", [False, True])
def test_poll_books_fills_before_risk_evaluates_new_intents(replay, reverse_batch):
    from talaria.account.ledger import read_journal

    scheme, data_dir = replay
    strategy = scheme.parent / "replay_strategy.py"
    strategy.write_text(strategy.read_text().replace(
        "self.triggered or bar.close != self.trigger_price",
        "bar.close not in (self.trigger_price, self.trigger_price + 1)",
    ))
    plugins = scheme.parent / "replay_plugins.py"
    plugins.write_text(plugins.read_text().replace(
        "        if self.deny:", """        if account_state.rounds:
            assert account_state.balances[("USDT", "POSITION_COST")] == Decimal("25.32")
            assert account_state.balances[("USDT", "CASH_AVAILABLE")] == Decimal(3)
            return RejectedPlanRecord(reason="TEST_BOOKED_BEFORE_RISK")
        if self.deny:""",
    ) + """
class BatchedFill(ExampleFill):
    def on_market(self, event, valuation):
        super().on_market(event, valuation)
        if not self.events:
            return
        funding = ExecutionEvent(
            kind="FUNDING_SETTLED", ts_event=event.as_of - 1, sub_account_id="example",
            postings=(
                LedgerPosting(account="CASH_AVAILABLE", currency="USDT", amount="3"),
                LedgerPosting(account="FUNDING", currency="USDT", amount="-3"),
            ),
        )
        self.events.insert(INDEX, funding)

fill = SlotPlugin(Slot.FILL, (1,), lambda context, params: BatchedFill(context, **params))
""".replace("INDEX", "1" if reverse_batch else "0"))
    assert launch(scheme, data_dir) == 0
    path, = (data_dir / "runs").glob("*/ledger.sqlite")
    journal = read_journal(path)
    assert [event.kind for event in journal.events] == [
        "FUNDING_SETTLED", "ENTRY_FILLED", "CANDIDATE_REJECTED",
    ]
    assert journal.events[-1].reason == "TEST_BOOKED_BEFORE_RISK"
    assert [event.ts_event for event in journal.events] == [
        START + 2 * MINUTE + 6_999, START + 2 * MINUTE + 7_000, START + 2 * MINUTE + 7_000,
    ]


@pytest.mark.parametrize("deny_add_on", [False, True])
def test_strategy_same_direction_intent_adds_to_its_round_through_risk(replay, deny_add_on):
    from talaria.account.ledger import read_journal

    scheme, data_dir = replay
    strategy = scheme.parent / "replay_strategy.py"
    strategy.write_text(strategy.read_text().replace(
        "self.triggered or bar.close != self.trigger_price",
        "bar.close not in (self.trigger_price, self.trigger_price + 1)",
    ))
    plugins = scheme.parent / "replay_plugins.py"
    plugins.write_text(plugins.read_text().replace(
        "        if self.deny:", """        if account_state.rounds:
            assert candidate.round_id == "example-round"
            assert candidate.invalidation_stop == Decimal(9)
            if DENY_ADD_ON:
                return RejectedPlanRecord(reason="TEST_ADD_ON_DENIED")
        if self.deny:""".replace("DENY_ADD_ON", repr(deny_add_on)),
    ).replace(
        "quantity=plan.sizing.quantity, stop=plan.candidate.invalidation_stop,",
        "quantity=plan.sizing.quantity * (2 if plan.candidate.round_id else 1), stop=plan.candidate.invalidation_stop,",
    ).replace("if event.bar.ts_event < plan.candidate.ts_decision:", "if event.as_of <= plan.candidate.ts_decision:"))
    assert launch(scheme, data_dir) == 0
    path, = (data_dir / "runs").glob("*/ledger.sqlite")
    first, second = read_journal(path).events
    assert first.kind == "ENTRY_FILLED" and first.position.quantity == 2
    if deny_add_on:
        assert (second.kind, second.reason) == ("CANDIDATE_REJECTED", "TEST_ADD_ON_DENIED")
    else:
        assert second.kind == "ENTRY_FILLED" and second.position.quantity == 4
        assert first.position.round_id == second.position.round_id
        assert first.plan_id != second.plan_id


@pytest.mark.parametrize(("slot", "params", "field"), [
    ("strategy", "trigger_price: 11", "trigger_price"),
    ("strategy", "trigger_price: '11', typo: 1", "typo"),
    ("strategy", "", "trigger_price"),
    ("risk", "deny: 'false'", "deny"),
    ("sizing", "typo: 1", "typo"),
    ("data", "start: '1793491200000'", "start"),
])
def test_account_refuses_invalid_yaml_slot_parameters(replay, capsys, slot, params, field):
    scheme, data_dir = replay
    content = scheme.read_text()
    if slot == "strategy":
        content = content.replace('trigger_price: "11"', params)
    elif slot == "data":
        content = content.replace("start: 1793491200000", params)
    else:
        content = content.replace(
            f"{slot}: replay_plugins.py:{slot}",
            f"{slot}: {{plugin: replay_plugins.py:{slot}, params: {{{params}}}}}",
        )
    scheme.write_text(content)
    assert launch(scheme, data_dir) == 2
    message = capsys.readouterr().err
    assert slot in message and field in message
    assert not (data_dir / "runs").exists()


def test_replaying_run_config_uses_recorded_effective_indicator_defaults(replay):
    import json
    import sqlite3

    from talaria.account.ledger import read_journal
    from talaria.perception.indicator_defaults import DEFAULTS_KEY
    from talaria.perception.sqlite import COLLABORATION_STATE_DB

    scheme, data_dir = replay
    strategy = scheme.parent / "replay_strategy.py"
    strategy.write_text(strategy.read_text().replace(
        "if self.triggered or bar.close != self.trigger_price:",
        "if self.triggered or ctx.indicator('SMA')[-1].values['value'] != self.trigger_price:",
    ))

    db_path = data_dir / COLLABORATION_STATE_DB
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO meta VALUES (?, ?)", (DEFAULTS_KEY, '{"SMA":{"period":1}}'))

    assert launch(scheme, data_dir) == 0
    first, = (data_dir / "runs").iterdir()
    artifact = first / "run-config.json"
    captured = json.loads(artifact.read_text())
    assert captured["indicator_defaults"]["SMA"]["period"] == 1
    assert captured["scheme"]["seed"] == 17
    first_journal = read_journal(first / "ledger.sqlite")
    assert [event.kind for event in first_journal.events] == ["ENTRY_FILLED"]

    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("UPDATE meta SET value = ? WHERE key = ?", ('{"SMA":{"period":5}}', DEFAULTS_KEY))
    assert launch(scheme, data_dir) == 0
    changed, = set((data_dir / "runs").iterdir()) - {first}
    assert read_journal(changed / "ledger.sqlite").events == ()

    scheme.unlink()
    assert main(["account", "run", str(artifact), "--mode", "backtest", "--replay"]) == 0
    repeated, = set((data_dir / "runs").iterdir()) - {first, changed}
    assert read_journal(repeated / "ledger.sqlite") == first_journal


def test_late_signal_bar_is_not_reused_by_immediate_fill(replay, capsys):
    scheme, data_dir = replay
    scheme.write_text(scheme.read_text().replace('trigger_price: "11"', 'trigger_price: "12"'))
    plugins = scheme.parent / "replay_plugins.py"
    plugins.write_text(plugins.read_text().replace(
        "if event.bar.ts_event < plan.candidate.ts_decision:",
        "if event.as_of < plan.candidate.ts_decision:",
    ) + """
class ImmediateFill(ExampleFill):
    def on_market(self, event, valuation):
        self.market = event

    def submit_plan(self, plan):
        receipt = super().submit_plan(plan)
        super().on_market(self.market, None)
        return receipt

fill = SlotPlugin(Slot.FILL, (1,), lambda context, params: ImmediateFill(context))
""")
    assert launch(scheme, data_dir) == 2
    assert "信号柱" in capsys.readouterr().err


def test_data_entry_that_publishes_a_bar_early_is_audited(replay, capsys):
    from talaria.account.ledger import read_journal

    scheme, data_dir = replay
    plugin = scheme.parent / "early_data.py"
    plugin.write_text("""\
from dataclasses import replace
from talaria.account.plugins.parquet import ParquetReplay, ReplayParams
from talaria.account.scheme import Slot, SlotPlugin

class EarlyData(ParquetReplay):
    def events(self):
        for event in super().events():
            yield replace(event, as_of=event.bar.ts_available - 1)

plugin = SlotPlugin(Slot.DATA, (1,), EarlyData, ReplayParams)
""")
    scheme.write_text(scheme.read_text().replace("parquet_replay", "early_data.py:plugin"))

    assert launch(scheme, data_dir) == 2
    assert "提前发布了行情" in capsys.readouterr().err
    path, = (data_dir / "runs").glob("*/ledger.sqlite")
    journal = read_journal(path)
    assert [event.kind for event in journal.events] == ["DATA_REJECTED"]
    assert journal.entries == ()
