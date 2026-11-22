"""trading/01 的交易方案 Schema 和插槽启动契约。"""

import pytest

pytestmark = [pytest.mark.small, pytest.mark.spec]

SCHEME = """\
scheme: breakout
interface_version: 2
sub_accounts: [alpha, beta]
slots:
  data: {plugin: parquet_replay, params: {instrument: BTC-USDT-PERP}}
  strategy: {python: strategy.py, params: {lookback: 2}}
  sizing: example
  risk: example
  fill: example
  tools: []
"""


def test_load_scheme_with_named_slots_and_python_strategy(tmp_path):
    from talaria.account.scheme import Slot, load_scheme

    path = tmp_path / "scheme.yaml"
    path.write_text(SCHEME)
    scheme = load_scheme(path)

    assert scheme.sub_accounts == ("alpha", "beta")
    assert scheme.slots.strategy.reference == "strategy.py:plugin"
    assert scheme.slots.strategy.params == {"lookback": 2}
    assert scheme.slots.selection(Slot.SIZING).reference == "example"
    assert scheme.slots.selection(Slot.DATA).params == {"instrument": "BTC-USDT-PERP"}


@pytest.mark.parametrize(("original", "invalid", "message"), [
    ("[alpha, beta]", "[alpha, alpha]", "重复"),
    ("[alpha, beta]", "[../alpha]", "sub_accounts"),
    ("interface_version: 2", "interface_version: 9", "interface_version"),
    ("interface_version: 2", "interface_version: true", "interface_version"),
    ("tools: []", "tools: [tool]", "tools"),
    ("risk: example", "risk: example\n  risk: replaced", "重复"),
])
def test_invalid_scheme_is_rejected(tmp_path, original, invalid, message):
    from talaria.account.scheme import SchemeError, load_scheme

    path = tmp_path / "scheme.yaml"
    path.write_text(SCHEME.replace(original, invalid))
    with pytest.raises(SchemeError, match=message):
        load_scheme(path)


def test_slot_rejects_incompatible_plugin_before_creating_it(tmp_path):
    from talaria.account.scheme import PluginRegistry, SchemeError, Slot, SlotPlugin

    def create(_context, _params):
        pytest.fail("不应创建不兼容的插件")

    registry = PluginRegistry()
    registry.register("example", SlotPlugin(Slot.RISK, (2,), create))
    with pytest.raises(SchemeError, match=r"risk.*example.*2.*1"):
        registry.resolve(Slot.RISK, "example", 1, tmp_path)


def test_strategy_file_resolution_requires_account_admission(tmp_path):
    from talaria.account.scheme import PluginRegistry, SchemeError, Slot

    (tmp_path / "test_strategy.py").write_text("raise AssertionError('意外加载')")
    with pytest.raises(SchemeError, match="账户宿主"):
        PluginRegistry().resolve(Slot.STRATEGY, "test_strategy.py:plugin", 1, tmp_path)


def test_scheme_selects_data_plugin_and_parameters_by_mode(tmp_path):
    from talaria.account.scheme import Slot, load_scheme

    path = tmp_path / "scheme.yaml"
    path.write_text(SCHEME.replace("data: {plugin: parquet_replay, params: {instrument: BTC-USDT-PERP}}",
        "data:\n    backtest: {plugin: parquet_replay, params: {start: 100}}\n"
        "    paper: {plugin: live_ws, params: {history_bars: 3}}"))
    slots = load_scheme(path).slots
    assert slots.selection(Slot.DATA, "paper").params == {"history_bars": 3}
    assert slots.selection(Slot.DATA, "backtest").reference == "parquet_replay"


def test_mode_switch_runs_both_data_plugins_from_one_yaml(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from talaria.account.host import default_registry, run_backtest, run_paper
    from talaria.account.plugins.live_ws import LiveWS
    from talaria.account.scheme import Slot, SlotPlugin
    from talaria.components import ComponentMeta
    scheme = tmp_path / "scheme.yaml"
    scheme.write_text("""scheme: dual
interface_version: 2
sub_accounts: [one]
slots:
  data:
    backtest: {plugin: parquet_replay, params: {instrument: BTC-USDT-PERP, timeframe: 1m, start: 0, end: 60000}}
    paper: {plugin: live_ws, params: {instrument: BTC-USDT-PERP, history_bars: 7}}
  strategy: idle
  sizing: r_unit
  risk: seven_step
  fill: bar_sim_1m
""")
    modes = []
    def idle(context, params):
        modes.append(context.mode)
        return SimpleNamespace(meta=ComponentMeta("idle", "1", (), {}, "strategy_decision"), on_bar=lambda ctx: None)
    def stream(data):
        assert data.params.history_bars == 7
        yield from ()
    monkeypatch.setattr(LiveWS, "events", stream)
    registry = default_registry()
    registry.register("idle", SlotPlugin(Slot.STRATEGY, (2,), idle))
    result = run_backtest(scheme, tmp_path, registry=registry)
    assert (result.directory / "ledger.sqlite").exists()
    run_paper(scheme, tmp_path, registry=registry)
    assert (tmp_path / "accounts/one/ledger.sqlite").exists()
    assert modes == ["backtest", "paper"]
