"""非默认市场包（如 OKX / 非 Bybit 标的）的端到端回测与目录解析契约。"""

from __future__ import annotations

from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path

import httpx
import pytest

from talaria.account.host import run_backtest
from talaria.account.ledger import read_journal
from talaria.account.strategy_code import StrategyCodeRegistry
from talaria.cli.account import _data_dir as cli_account_data_dir
from talaria.foundation.connector import RawBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.runs import RunExportSummary
from talaria.packs import DEFAULT_PACK, get_market_pack, resolve_market_pack, traits_for_spec
from talaria.perception.cold_store import ColdBatch, ColdStore
from talaria.service_endpoint import DATA_DIR_HEADER, data_dir_id, default_data_dir, service_descriptor_path
from talaria.service_lock import ServiceLock

pytestmark = [pytest.mark.medium, pytest.mark.spec]

START = 1_793_491_200_000  # 2026-11-01 00:00:00 UTC
MINUTE = 60_000


def _okx_doge_spec() -> InstrumentSpec:
    return InstrumentSpec(
        instrument_id="DOGE-USDT-PERP",
        venue="okx",
        symbol="DOGE-USDT-SWAP",
        asset_class="crypto_perp",
        tick_size=Decimal("0.0001"),
        qty_step=Decimal("0.0001"),
        min_qty=Decimal("0.0001"),
        price_precision=4,
        qty_precision=4,
    )


def _bar(symbol: str, ts: int, o: str, h: str, l: str, c: str, vol: str = "10000") -> RawBar:
    available = ts + MINUTE
    return RawBar(
        symbol=symbol,
        timeframe="1m",
        ts_event=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(l),
        close=Decimal(c),
        volume=Decimal(vol),
        turnover=Decimal(c) * Decimal(vol),
        ts_available=available,
        ts_ingested=available,
        ts_committed=available,
        revision=0,
    )


def test_non_default_pack_okx_backtest_end_to_end(tmp_path: Path) -> None:
    """使用不在 DEFAULT_PACK 中的 OKX DOGE-USDT-PERP 跑通回测、账本与 export_run。"""
    spec = _okx_doge_spec()
    assert get_market_pack(DEFAULT_PACK).instrument(spec.instrument_id) is None

    data_dir = tmp_path / "data"
    with closing(ColdStore(data_dir)) as store:
        store.publish(
            ColdBatch(
                spec,
                "1m",
                [
                    _bar(spec.symbol, START, "0.1000", "0.1002", "0.0998", "0.1000"),
                    _bar(spec.symbol, START + MINUTE, "0.1000", "0.1002", "0.0999", "0.1001"),
                    _bar(spec.symbol, START + 2 * MINUTE, "0.1001", "0.1060", "0.1000", "0.1055"),
                ],
            )
        )
        marks = [
            DerivativePoint(
                instrument_id=spec.instrument_id,
                kind="mark_price",
                value=price,
                ts_event=ts,
                ts_available=ts + MINUTE,
                ts_ingested=ts + MINUTE,
                ts_committed=ts + MINUTE,
            )
            for ts, price in (
                (START, "0.1000"),
                (START + MINUTE, "0.1001"),
                (START + 2 * MINUTE, "0.1055"),
            )
        ]
        store.publish_derivatives(spec, DerivativeKind.MARK_PRICE, marks)

    scheme_dir = tmp_path / "scheme"
    scheme_dir.mkdir()
    strategy_path = scheme_dir / "doge_strategy.py"
    strategy_path.write_text(
        """from decimal import Decimal
from talaria.account.scheme import Slot, SlotPlugin
from talaria.components import ComponentMeta, Ctx
from talaria.foundation.collaboration.contracts import Abstain, AbstainReason, SetupInsight


class DogeStrategy:
    meta = ComponentMeta(
        name="doge_replay",
        version="1",
        inputs=("bars",),
        parameters_schema={"type": "object", "properties": {}},
        output_type="strategy_decision",
    )

    def __init__(self) -> None:
        self.triggered = False

    def on_bar(self, ctx: Ctx):
        bar = ctx.bars()[-1]
        if self.triggered or bar.close != Decimal("0.1000"):
            return Abstain(reason_code=AbstainReason.EVIDENCE_CONFLICT, message="wait")
        self.triggered = True
        return SetupInsight(
            direction="LONG",
            summary="enter okx doge",
            entry_low=Decimal("0.0999"),
            entry_high=Decimal("0.1001"),
            invalidation_stop=Decimal("0.0980"),
            take_profit=[Decimal("0.1050")],
        )


def make_strategy(_context, _params):
    return DogeStrategy()


plugin = SlotPlugin(Slot.STRATEGY, (2,), make_strategy, DogeStrategy.meta.parameters_schema)
""",
        encoding="utf-8",
    )
    scheme_path = scheme_dir / "scheme.yaml"
    scheme_path.write_text(
        f"""scheme: okx-doge-backtest
interface_version: 2
sub_accounts: [main]
seed: 7
slots:
  data:
    plugin: parquet_replay
    params:
      instrument: DOGE-USDT-PERP
      timeframe: 1m
      start: {START}
      end: {START + 3 * MINUTE}
  strategy: {{python: doge_strategy.py, params: {{}}}}
  sizing: r_unit
  risk: seven_step
  fill: bar_sim_1m
  tools: []
""",
        encoding="utf-8",
    )
    StrategyCodeRegistry(data_dir).register(strategy_path, "human")

    run = run_backtest(scheme_path, data_dir)
    assert run.read_completion().status == "succeeded"

    journal = read_journal(run.directory / "ledger.sqlite")
    kinds = [event.kind for event in journal.events]
    assert "ENTRY_FILLED" in kinds
    assert "EXIT_FILLED" in kinds
    filled_instruments = {
        event.position.instrument_id
        for event in journal.events
        if event.kind in {"ENTRY_FILLED", "EXIT_FILLED"} and event.position is not None
    }
    assert filled_instruments == {"DOGE-USDT-PERP"}

    summary = RunExportSummary.model_validate_json(
        (run.directory / "summary.json").read_text(encoding="utf-8")
    )
    assert summary.status == "succeeded"
    assert (run.directory / "rounds.parquet").is_file()
    assert (run.directory / "equity.parquet").is_file()


def test_traits_for_spec_covers_asset_classes_and_fallback() -> None:
    perp = _okx_doge_spec()
    perp_traits = traits_for_spec(perp)
    assert perp_traits == get_market_pack(DEFAULT_PACK).traits("BTC-USDT-PERP")

    spot = InstrumentSpec(
        instrument_id="BTC-USDT-SPOT",
        venue="okx",
        symbol="BTC-USDT",
        asset_class="crypto_spot",
        tick_size=Decimal("0.1"),
        qty_step=Decimal("0.0001"),
        min_qty=Decimal("0.0001"),
        price_precision=1,
        qty_precision=4,
    )
    spot_traits = traits_for_spec(spot)
    assert spot_traits.margin.margin_asset == "USDT"
    assert spot_traits.margin.max_leverage == Decimal("1")
    assert spot_traits.carry.funding_interval_hours is None
    assert spot_traits.session.session_kind == "24x7"
    assert spot_traits.order_constraint.post_only is True
    assert spot_traits.derivatives is None

    equity = InstrumentSpec(
        instrument_id="AAPL-USD",
        venue="nasdaq",
        symbol="AAPL",
        asset_class="us_equity",
        tick_size=Decimal("0.01"),
        qty_step=Decimal("1"),
        min_qty=Decimal("1"),
        price_precision=2,
        qty_precision=0,
    )
    equity_traits = traits_for_spec(equity)
    assert equity_traits.margin.margin_asset == "USD"
    assert equity_traits.margin.max_leverage == Decimal("4")
    assert equity_traits.carry.funding_interval_hours is None
    assert equity_traits.session.session_kind == "regular"
    assert equity_traits.session.timezone == "America/New_York"
    assert equity_traits.session.day_start == "09:30"
    assert len(equity_traits.session.sessions) == 1
    assert equity_traits.session.sessions[0].start == "09:30"
    assert equity_traits.session.sessions[0].end == "16:00"
    assert equity_traits.order_constraint.post_only is False
    assert equity_traits.derivatives is None

    other = equity.model_copy(update={"instrument_id": "CUSTOM-1", "asset_class": "fx_spot"})
    assert traits_for_spec(other) == spot_traits


def test_catalog_market_pack_offline_override_and_online_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    okx_btc = InstrumentSpec(
        instrument_id="BTC-USDT-PERP",
        venue="okx",
        symbol="BTC-USDT-SWAP",
        asset_class="crypto_perp",
        tick_size=Decimal("0.1"),
        qty_step=Decimal("0.0001"),
        min_qty=Decimal("0.0001"),
        price_precision=1,
        qty_precision=4,
    )
    with closing(ColdStore(tmp_path)) as store:
        store.publish(
            ColdBatch(
                okx_btc,
                "1m",
                [_bar(okx_btc.symbol, START, "50000", "50100", "49900", "50050", "1.0")],
            )
        )

    offline_pack = resolve_market_pack(tmp_path, offline=True)
    resolved_btc = offline_pack.instrument("BTC-USDT-PERP")
    assert resolved_btc is not None
    assert resolved_btc.venue == "okx"
    assert resolved_btc.symbol == "BTC-USDT-SWAP"
    # 离线模式对冷库未收录标的仍回退默认包。
    assert offline_pack.instrument("ETH-USDT-PERP") is not None

    okx_doge = _okx_doge_spec()

    def respond(_transport, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/instruments":
            return httpx.Response(
                200,
                headers={DATA_DIR_HEADER: data_dir_id(tmp_path)},
                json=[okx_doge.model_dump(mode="json")],
            )
        pytest.fail(f"unexpected request to {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    with ServiceLock(tmp_path) as lock:
        descriptor = service_descriptor_path(tmp_path)
        descriptor.parent.mkdir(exist_ok=True)
        descriptor.write_text(
            json.dumps({"version": 1, "base_url": "http://127.0.0.1:8765", "pid": lock.pid}),
            encoding="utf-8",
        )
        online_pack = resolve_market_pack(tmp_path, offline=False)

    assert online_pack.instrument("DOGE-USDT-PERP") == okx_doge
    assert online_pack.instrument("BTC-USDT-PERP") == okx_btc
    # 在线目录返回 200 时不再静默回退默认 Bybit 包未配置标的。
    assert online_pack.instrument("ETH-USDT-PERP") is None
    with pytest.raises(KeyError):
        online_pack.traits("ETH-USDT-PERP")


def test_cli_account_default_data_dir_uses_home_talaria_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("TALARIA_DATA_DIR", raising=False)
    assert cli_account_data_dir() == (fake_home / ".talaria" / "data").resolve()
    assert default_data_dir() == (fake_home / ".talaria" / "data").resolve()

    env_dir = tmp_path / "custom-data"
    monkeypatch.setenv("TALARIA_DATA_DIR", str(env_dir))
    assert cli_account_data_dir() == env_dir.resolve()
