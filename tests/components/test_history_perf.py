"""Strict equivalence and linear scaling tests for incremental replay runtime."""

from __future__ import annotations

from contextlib import closing
from decimal import Decimal
from functools import partial
import time
from uuid import UUID

import pytest

from talaria import sdk
from talaria.account.host import run_backtest
from talaria.account.ledger import read_journal
from talaria.account.plugins.parquet import TickReplay
from talaria.account.scheme import Slot, SlotPlugin
from talaria.account.strategy_code import StrategyCodeRegistry
from talaria.components import ComponentMeta, Ctx, run_history
from talaria.foundation.bars import RangeBar
from talaria.foundation.collaboration.contracts import Abstain, AbstainReason, SetupInsight
from talaria.foundation.connector import RawBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.ticks import TickKind, Trade
from talaria.foundation.timeframes import timeframe_ms
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.perception.cold_store import ColdBatch, ColdReader, ColdStore, partition_day, partition_month

pytestmark = [pytest.mark.medium, pytest.mark.spec]

INSTRUMENT = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
MINUTE = 60_000
FIVE_MIN = 5 * MINUTE
DAY_MS = 86_400_000
START = 1_767_225_600_000  # 2026-01-01T00:00:00Z


def _raw_bar(
    ts: int,
    open_p: str,
    high_p: str,
    low_p: str,
    close_p: str,
    volume: str,
    *,
    timeframe: str = "1m",
    delay: int = 0,
    revision: int = 0,
) -> RawBar:
    avail = ts + timeframe_ms(timeframe) + delay
    vol = Decimal(volume)
    close_d = Decimal(close_p)
    return RawBar(
        symbol=INSTRUMENT.symbol,
        timeframe=timeframe,
        ts_event=ts,
        open=Decimal(open_p),
        high=Decimal(high_p),
        low=Decimal(low_p),
        close=close_d,
        volume=vol,
        turnover=close_d * vol,
        ts_available=avail,
        ts_ingested=avail,
        ts_committed=avail,
        revision=revision,
    )


def _deriv(ts: int, avail: int, value: str, *, kind: DerivativeKind, timeframe: str | None = None,
           settled: bool = True, revision: int = 0) -> DerivativePoint:
    return DerivativePoint(
        instrument_id=INSTRUMENT.instrument_id,
        kind=kind,
        timeframe=timeframe,
        ts_event=ts,
        ts_available=avail,
        ts_ingested=avail,
        ts_committed=avail,
        ts_published=ts if kind is DerivativeKind.FUNDING_RATE and settled else None,
        value=Decimal(value),
        revision=revision,
    )


def _build_edge_case_store(data_dir) -> tuple[int, int]:
    """Create ColdStore spanning midnight with NO_TRADES, FEED_GAP, late bars, revisions, and 5m/derivatives."""
    day1_end = START + DAY_MS
    anchor = day1_end - 12 * MINUTE  # 12 bars on day 1, then crosses UTC midnight into day 2
    bars_1m = [
        _raw_bar(anchor - 5 * MINUTE, "99", "101", "98", "100", "5"),
        _raw_bar(anchor, "100", "102", "99", "101", "10"),
        # Revision to bar 0 arriving later
        _raw_bar(anchor, "100", "103", "99", "102", "12", delay=4 * MINUTE + 500, revision=1),
        # NO_TRADES flat zero-volume bar
        _raw_bar(anchor + 1 * MINUTE, "101", "101", "101", "101", "0"),
        # Normal bar
        _raw_bar(anchor + 2 * MINUTE, "101", "105", "100", "104", "8"),
        # Slot anchor + 3m is initially missing (FEED_GAP when anchor+4m arrives), then arrives late!
        _raw_bar(anchor + 3 * MINUTE, "104", "106", "103", "105", "6", delay=3 * MINUTE + 100),
        _raw_bar(anchor + 4 * MINUTE, "104", "107", "103", "106", "7"),
        # Permanent FEED_GAP at anchor + 5m and 6m
        _raw_bar(anchor + 7 * MINUTE, "106", "108", "105", "107", "9"),
        # Revision on anchor + 7m
        _raw_bar(anchor + 7 * MINUTE, "106", "109", "105", "108", "11", delay=2 * MINUTE, revision=1),
    ]
    # Continue across midnight into day 2
    for step in range(8, 22):
        ts = anchor + step * MINUTE
        c = 108 + (step % 5)
        vol = "0" if step == 14 else str(5 + step % 4)
        o_p = str(c) if vol == "0" else str(c - 1)
        h_p = str(c) if vol == "0" else str(c + 2)
        l_p = str(c) if vol == "0" else str(c - 2)
        bars_1m.append(_raw_bar(ts, o_p, h_p, l_p, str(c), vol))

    bars_5m = [
        _raw_bar(anchor - 10 * MINUTE, "95", "100", "94", "99", "20", timeframe="5m"),
        _raw_bar(anchor - 5 * MINUTE, "99", "102", "98", "101", "25", timeframe="5m"),
        _raw_bar(anchor, "101", "107", "100", "106", "30", timeframe="5m"),
        _raw_bar(anchor, "101", "108", "100", "107", "32", timeframe="5m", delay=2 * MINUTE, revision=1),
        _raw_bar(anchor + 5 * MINUTE, "106", "109", "105", "108", "28", timeframe="5m"),
        _raw_bar(anchor + 10 * MINUTE, "108", "112", "107", "111", "35", timeframe="5m"),
        _raw_bar(anchor + 15 * MINUTE, "111", "114", "110", "112", "40", timeframe="5m"),
    ]

    funding = [
        _deriv(anchor, anchor, "0.0001", kind=DerivativeKind.FUNDING_RATE),
        _deriv(anchor + 5 * MINUTE, anchor + 5 * MINUTE, "0.0003", kind=DerivativeKind.FUNDING_RATE),
        _deriv(anchor + 5 * MINUTE, anchor + 8 * MINUTE + 50, "0.0005", kind=DerivativeKind.FUNDING_RATE, revision=1),
        _deriv(anchor + 10 * MINUTE, anchor + 10 * MINUTE, "-0.0002", kind=DerivativeKind.FUNDING_RATE),
    ]
    oi = [
        _deriv(anchor, anchor, "1000", kind=DerivativeKind.OPEN_INTEREST, timeframe="5m"),
        _deriv(anchor + 5 * MINUTE, anchor + 5 * MINUTE, "1050", kind=DerivativeKind.OPEN_INTEREST, timeframe="5m"),
        _deriv(anchor + 10 * MINUTE, anchor + 10 * MINUTE, "1020", kind=DerivativeKind.OPEN_INTEREST, timeframe="5m"),
    ]

    with closing(ColdStore(data_dir)) as store:
        store.publish(ColdBatch(INSTRUMENT, "1m", bars_1m))
        store.publish(ColdBatch(INSTRUMENT, "5m", bars_5m))
        store.publish_derivatives(INSTRUMENT, DerivativeKind.FUNDING_RATE, funding)
        store.publish_derivatives(INSTRUMENT, DerivativeKind.OPEN_INTEREST, oi)

    return anchor, anchor + 23 * MINUTE


def _eval_all_indicators(ctx: Ctx) -> dict[str, object]:
    return {
        "bars": tuple(ctx.bars()),
        "bars_5m": tuple(ctx.bars("5m")),
        "sma": tuple(ctx.indicator("SMA", period=3)),
        "ema": tuple(ctx.indicator("EMA", period=3)),
        "rsi": tuple(ctx.indicator("RSI", period=3)),
        "atr": tuple(ctx.indicator("ATR", period=3)),
        "boll": tuple(ctx.indicator("BOLL", period=3, std_dev="2.5")),
        "vwap_session": tuple(ctx.indicator("VWAP", anchor="session")),
        "vwap_snapshot": tuple(ctx.indicator("VWAP", anchor="snapshot")),
        "sma_5m": tuple(ctx.indicator("SMA", timeframe="5m", period=2)),
        "ema_5m": tuple(ctx.indicator("EMA", timeframe="5m", period=2)),
        "legacy_ema": tuple(ctx.indicator("legacy_ema", period=3)),
        "legacy_atr": tuple(ctx.indicator("legacy_atr", period=3)),
        "funding_q": tuple(ctx.indicator("FUNDING_QUANTILE", period=2)),
        "oi_div": tuple(ctx.indicator("OI_DIVERGENCE", period=1)),
        "net_odds": tuple(ctx.indicator("NET_ODDS", period=2)),
    }


def test_incremental_indicator_matches_full_recompute_across_edge_cases(tmp_path):
    """Incremental Ctx/ColdBarCache matches full uncached recompute across revisions, FEED_GAP, NO_TRADES, VWAP, MTF."""
    start_ts, end_ts = _build_edge_case_store(tmp_path)
    history_start = start_ts - 5 * MINUTE

    # Incremental run (ColdBarCache + OperatorCursor + _first_confirmation cache)
    incremental = list(run_history(
        _eval_all_indicators, INSTRUMENT.instrument_id, "1m",
        start=start_ts, end=end_ts, history_start=history_start, data_dir=tmp_path,
    ))

    # Reference uncached run: wrap sdk.bars in a plain function so it bypasses _ColdCachedReader
    # and evaluate each step inside an isolated Ctx without _replay_session.
    def plain_read_bars(instrument, timeframe, *, start, end, as_of):
        return sdk.bars(instrument, timeframe, start=start, end=end, as_of=as_of, data_dir=tmp_path, offline=True)

    def eval_uncached(ctx: Ctx) -> dict[str, object]:
        fresh = Ctx(
            ctx.instrument, ctx.timeframe, ctx.start, ctx.now,
            _read_bars=plain_read_bars,
            _read_derivatives=ctx._read_derivatives,
            _data_dir=ctx._data_dir,
            _bar_ts=ctx._bar_ts,
            _discovery_tail_completed=ctx._discovery_tail_completed,
            _indicator_defaults=ctx._indicator_defaults,
            _replay_session=None,
        )
        return _eval_all_indicators(fresh)

    reference = list(run_history(
        eval_uncached, INSTRUMENT.instrument_id, "1m",
        start=start_ts, end=end_ts, history_start=history_start, read_bars=plain_read_bars, data_dir=tmp_path,
    ))

    assert len(incremental) == len(reference) > 15
    for idx, (inc_step, ref_step) in enumerate(zip(incremental, reference, strict=True)):
        assert inc_step.as_of == ref_step.as_of, f"step {idx} as_of mismatch"
        assert inc_step.tail_completed == ref_step.tail_completed, f"step {idx} tail_completed mismatch"
        for key in ref_step.output:
            assert inc_step.output[key] == ref_step.output[key], f"step {idx} key {key!r} mismatch"


def test_parquet_replay_incremental_matches_uncached_reference(tmp_path):
    """Full ParquetReplay + BarSim1m ledger matches uncached Ctx reference under revisions and gaps."""
    data_dir = tmp_path / "data"
    start_ts, end_ts = _build_edge_case_store(data_dir)
    scheme_dir = tmp_path / "scheme"
    scheme_dir.mkdir(parents=True)

    strategy_file = scheme_dir / "strat.py"
    strategy_file.write_text("""\
from decimal import Decimal
from talaria.account.scheme import Slot, SlotPlugin
from talaria.components import ComponentMeta, Ctx
from talaria.foundation.collaboration.contracts import Abstain, AbstainReason, SetupInsight

class Strat:
    meta = ComponentMeta("eq_strat", "1", ("bars",), {"type": "object", "properties": {}}, "strategy_decision")
    def __init__(self):
        self.entered = False
    def on_bar(self, ctx: Ctx):
        ema = ctx.indicator("EMA", period=3)[-1].values["value"]
        rsi = ctx.indicator("RSI", period=3)[-1].values["value"]
        atr = ctx.indicator("ATR", period=3)[-1].values["value"]
        if not self.entered and ema is not None and rsi is not None and atr is not None and atr > 0:
            self.entered = True
            bar = ctx.bars()[-1]
            return SetupInsight(
                direction="LONG", summary="entry",
                entry_low=bar.close - Decimal("1"), entry_high=bar.close + Decimal("1"),
                invalidation_stop=bar.close - Decimal("10"), take_profit=[bar.close + Decimal("5")],
            )
        return Abstain(reason_code=AbstainReason.EVIDENCE_CONFLICT, message="hold")

plugin = SlotPlugin(Slot.STRATEGY, (2,), lambda _ctx, _p: Strat(), Strat.meta.parameters_schema)
""", encoding="utf-8")
    StrategyCodeRegistry(data_dir).register(strategy_file, "human")

    scheme_yaml = scheme_dir / "scheme.yaml"
    scheme_yaml.write_text(f"""\
scheme: eq-replay
interface_version: 2
sub_accounts: [acct]
seed: 7
slots:
  data:
    plugin: parquet_replay
    params:
      instrument: BTC-USDT-PERP
      timeframe: 1m
      start: {start_ts}
      end: {end_ts}
  strategy: {{python: strat.py, params: {{}}}}
  sizing: r_unit
  risk: seven_step
  fill: bar_sim_1m
  tools: []
""", encoding="utf-8")

    run_inc = run_backtest(scheme_yaml, data_dir)
    journal_inc = read_journal(run_inc.directory / "ledger.sqlite")

    # Now wrap parquet_replay in a plain Data plugin whose read_bars bypasses _ColdCachedReader
    uncached_data = scheme_dir / "uncached_data.py"
    uncached_data.write_text("""\
from dataclasses import replace
from talaria.account.contracts import MarketData
from talaria.account.plugins.parquet import ParquetReplay, ReplayParams
from talaria.account.scheme import Slot, SlotPlugin

class UncachedParquet(ParquetReplay):
    def events(self, **kwargs):
        for event in super().events(**kwargs):
            if event.source is not None:
                orig = event.source.read_bars
                plain = lambda *a, _orig=orig, **kw: _orig(*a, **kw)
                yield replace(event, source=replace(event.source, read_bars=plain))
            else:
                yield event

plugin = SlotPlugin(Slot.DATA, (2,), UncachedParquet, ReplayParams)
""", encoding="utf-8")
    StrategyCodeRegistry(data_dir).register(uncached_data, "human")
    uncached_scheme = scheme_dir / "scheme_uncached.yaml"
    uncached_scheme.write_text(
        scheme_yaml.read_text(encoding="utf-8").replace("plugin: parquet_replay", "plugin: uncached_data.py:plugin"),
        encoding="utf-8",
    )
    run_ref = run_backtest(uncached_scheme, data_dir)
    journal_ref = read_journal(run_ref.directory / "ledger.sqlite")

    assert journal_inc.events == journal_ref.events
    assert journal_inc.entries == journal_ref.entries


def test_tick_replay_covers_multi_day_matches_five_minute_reference(tmp_path):
    """Multi-day 1d-paged TickReplay.covers() matches single-bar 5m-paged reference across revisions & gaps."""
    bars = [
        _raw_bar(START + i * MINUTE, "100", "105", "95", "102", str(10 + i))
        for i in range(10)
    ]
    # Add bars on day 2
    day2 = START + DAY_MS
    bars.extend(
        _raw_bar(day2 + i * MINUTE, "110", "115", "105", "112", str(20 + i))
        for i in range(6)
    )
    trades: list[Trade] = []
    seq = 1
    for idx, bar in enumerate(bars):
        if idx % 3 == 0:
            # Exact coverage with 2 trades
            t1 = Trade(
                instrument_id=INSTRUMENT.instrument_id, ts_event=bar.ts_event + 1000,
                ts_available=bar.ts_event + 1000, ts_ingested=bar.ts_event + 1000, ts_committed=bar.ts_event + 1000,
                trade_id=UUID(int=seq), price=bar.low, size=bar.volume / Decimal(2), side="Buy", sequence=seq,
            )
            seq += 1
            t2 = Trade(
                instrument_id=INSTRUMENT.instrument_id, ts_event=bar.ts_event + 2000,
                ts_available=bar.ts_event + 2000, ts_ingested=bar.ts_event + 2000, ts_committed=bar.ts_event + 2000,
                trade_id=UUID(int=seq), price=bar.high, size=bar.volume - bar.volume / Decimal(2),
                side="Sell", sequence=seq,
            )
            seq += 1
            trades.extend([t1, t2])
        elif idx % 3 == 1:
            # Partial coverage (wrong volume sum) + a late revision whose earliest ts_available still covers or not
            t1 = Trade(
                instrument_id=INSTRUMENT.instrument_id, ts_event=bar.ts_event + 1000,
                ts_available=bar.ts_event + 1000, ts_ingested=bar.ts_event + 1000, ts_committed=bar.ts_event + 1000,
                trade_id=UUID(int=seq), price=bar.low, size=Decimal("1"), side="Buy", sequence=seq,
            )
            seq += 1
            trades.append(t1)
        # idx % 3 == 2: no trades at all

    with closing(ColdStore(tmp_path)) as store:
        by_month: dict[str, list[RawBar]] = {}
        for b in bars:
            by_month.setdefault(partition_month(b.ts_event), []).append(b)
        for mb in by_month.values():
            store.publish(ColdBatch(INSTRUMENT, "1m", mb))
        by_day: dict[str, list[Trade]] = {}
        for tr in trades:
            by_day.setdefault(partition_day(tr.ts_event), []).append(tr)
        for dt in by_day.values():
            store.publish_ticks(INSTRUMENT, TickKind.TRADES, dt)

    reader = ColdReader(tmp_path)
    range_bars = [RangeBar.model_validate(b, from_attributes=True) for b in reader.read_bars(INSTRUMENT, "1m")]
    multi = TickReplay(reader, INSTRUMENT.instrument_id, range_bars)
    for b in range_bars:
        single = TickReplay(reader, INSTRUMENT.instrument_id, (b,))
        assert multi.covers(b.ts_event) == single.covers(b.ts_event)


def test_run_history_linear_scaling(tmp_path):
    """run_history scales linearly across 10k vs 30k 1m bars (ratio << quadratic 9x)."""
    n_large = 30_000
    bars = []
    price = Decimal("50000")
    for i in range(n_large):
        ts = START + i * MINUTE
        delta = Decimal((i % 11) - 5) * Decimal("2")
        close_p = max(Decimal("100"), price + delta)
        avail = ts + MINUTE
        bars.append(RawBar(
            symbol=INSTRUMENT.symbol, timeframe="1m", ts_event=ts,
            open=price, high=max(price, close_p) + Decimal("5"), low=min(price, close_p) - Decimal("5"),
            close=close_p, volume=Decimal("10"), turnover=close_p * Decimal("10"),
            ts_available=avail, ts_ingested=avail, ts_committed=avail, revision=0,
        ))
        price = close_p

    with closing(ColdStore(tmp_path)) as store:
        by_month: dict[str, list[RawBar]] = {}
        for b in bars:
            by_month.setdefault(partition_month(b.ts_event), []).append(b)
        for mb in by_month.values():
            store.publish(ColdBatch(INSTRUMENT, "1m", mb))

    def eval_three(ctx: Ctx):
        return (
            ctx.indicator("EMA", period=20)[-1].values["value"],
            ctx.indicator("RSI", period=14)[-1].values["value"],
            ctx.indicator("ATR", period=14)[-1].values["value"],
        )

    def time_run(count: int) -> float:
        end_ts = START + count * MINUTE
        t0 = time.perf_counter()
        out = list(run_history(eval_three, INSTRUMENT.instrument_id, "1m", start=START, end=end_ts, data_dir=tmp_path))
        elapsed = time.perf_counter() - t0
        assert len(out) == count
        assert out[-1].output[0] is not None
        return elapsed

    t_10k = time_run(10_000)
    t_30k = time_run(30_000)
    ratio = t_30k / max(t_10k, 1e-6)
    assert ratio < 4.5, f"Expected linear (~3x) scaling, got {ratio:.2f}x ({t_10k:.3f}s -> {t_30k:.3f}s)"
