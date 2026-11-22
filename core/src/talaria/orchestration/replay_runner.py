"""批量启动离线账户进程，按输入顺序汇总其账本。"""

from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Literal

from talaria import sdk
from talaria.account.host import FilePluginCode, RunCompletion, RunConfig, RunResult, resolve_run_config
from talaria.account.ledger import read_journal
from talaria.account.plugins.parquet import ReplayParams, capture_replay_snapshot
from talaria.account.scheme import PluginSelection, SchemeError, Slot
from talaria.foundation.errors import TalariaError
from talaria.foundation.types import ContractModel, DecimalStr, decimal_from_finite_fraction


class CurrencyResult(ContractModel):
    """同币种子账户的汇总金额，资金费为账户净收付，回撤为绝对金额。"""

    net_pnl: DecimalStr
    max_drawdown: DecimalStr
    fees: DecimalStr
    funding: DecimalStr
    liquidation_fees: DecimalStr


class ReplayResult(RunCompletion):
    scheme: str
    scheme_path: Path
    fill_plugin: str
    start: int
    end: int
    directory: Path
    empty_sample: bool | None = None
    trade_count: int | None = None
    by_currency: dict[str, CurrencyResult] | None = None


class FillComparison(ContractModel):
    """同一方案与区间中逐笔插件减 1m 插件的账本差值。"""

    l1_run_id: str
    bar_run_id: str
    trade_count_delta: int | None = None
    by_currency_delta: dict[str, CurrencyResult] | None = None


class ReplayReport(ContractModel):
    strategy_scope: Literal["deterministic_only"] = "deterministic_only"
    pnl_basis: Literal["booked"] = "booked"
    runs: list[ReplayResult]
    fill_comparisons: list[FillComparison]


@dataclass
class _BookedResult:
    net_pnl: Fraction = Fraction(0)
    peak_pnl: Fraction = Fraction(0)
    max_drawdown: Fraction = Fraction(0)
    fees: Fraction = Fraction(0)
    funding: Fraction = Fraction(0)
    liquidation_fees: Fraction = Fraction(0)

    def book(self, *, pnl: Fraction, fees: Fraction, funding: Fraction, liquidation_fees: Fraction) -> None:
        self.fees += fees
        self.funding += funding
        self.liquidation_fees += liquidation_fees
        self.net_pnl += pnl - fees + funding - liquidation_fees
        self.peak_pnl = max(self.peak_pnl, self.net_pnl)
        self.max_drawdown = max(self.max_drawdown, self.peak_pnl - self.net_pnl)

    def result(self) -> CurrencyResult:
        return CurrencyResult(
            net_pnl=decimal_from_finite_fraction(self.net_pnl),
            max_drawdown=decimal_from_finite_fraction(self.max_drawdown),
            fees=decimal_from_finite_fraction(self.fees),
            funding=decimal_from_finite_fraction(self.funding),
            liquidation_fees=decimal_from_finite_fraction(self.liquidation_fees),
        )


def summarize_ledger(path: Path) -> tuple[int, dict[str, CurrencyResult]]:
    """按分录顺序整笔推进已实现损益曲线，按归零的出场成交计仓位回合。"""
    journal = read_journal(path)
    totals = defaultdict(_BookedResult)
    for entry in journal.entries:
        by_currency = defaultdict(lambda: defaultdict(Fraction))
        for posting in entry.postings:
            by_currency[posting.currency][posting.account] += Fraction(posting.amount)
        for currency, postings in by_currency.items():
            totals[currency].book(
                pnl=-postings["REALIZED_PNL"], fees=postings["EXPENSE_EXECUTION_FEE"],
                funding=-postings["FUNDING"], liquidation_fees=postings["EXPENSE_LIQUIDATION_FEE"],
            )
    trades = sum(
        event.kind == "EXIT_FILLED" and event.position is not None and event.position.quantity == 0
        for event in journal.events
    )
    return trades, {currency: totals[currency].result() for currency in sorted(totals)}


@dataclass(frozen=True, slots=True)
class _ReplayJob:
    scheme_path: Path
    config: RunConfig
    params: ReplayParams

    def run(self) -> ReplayResult:
        run = RunResult.new(self.config.data_dir)
        trades = by_currency = None
        empty_sample = None
        prepared = False
        try:
            run.prepare(self.config)
            prepared = True
            process = subprocess.run(
                [sys.executable, "-m", "talaria", "account", "run", str(run.config_path),
                 "--mode", "backtest", "--replay", "--run-id", run.run_id],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            )
            try:
                completion = run.read_completion()
            except (OSError, ValueError) as exc:
                if process.returncode:
                    reason = process.stderr.strip() or f"账户进程退出码 {process.returncode}"
                else:
                    reason = f"无法读取账户进程完成状态：{exc}"
                raise SchemeError(reason) from exc
            if completion.status == "succeeded":
                if process.returncode:
                    raise SchemeError(f"账户进程退出码 {process.returncode}：{process.stderr.strip()}")
                if completion.signal_bars is None:
                    raise SchemeError("账户进程完成状态缺少信号柱数")
                if completion.signal_bars:
                    trades, by_currency = summarize_ledger(run.directory / "ledger.sqlite")
                empty_sample = completion.signal_bars == 0
        except (OSError, sqlite3.Error, TalariaError, ValueError) as exc:
            error = str(exc) or type(exc).__name__
            completion = RunCompletion(run_id=run.run_id, status="failed", error=error)
            if prepared:
                try:
                    run.mark_failed(error)
                except OSError as status_error:
                    completion.error = f"{error}。写入完成状态失败：{status_error}"
        return ReplayResult(
            scheme=self.config.scheme.scheme, scheme_path=self.scheme_path,
            fill_plugin=self.config.scheme.slots.selection(Slot.FILL).reference,
            start=self.params.start, end=self.params.end,
            **completion.model_dump(), directory=run.directory, empty_sample=empty_sample,
            trade_count=trades, by_currency=by_currency,
        )


def _compare_fills(l1: ReplayResult, bar: ReplayResult) -> FillComparison:
    comparison = FillComparison(l1_run_id=l1.run_id, bar_run_id=bar.run_id)
    if l1.trade_count is None or bar.trade_count is None:
        return comparison
    l1_amounts, bar_amounts = l1.by_currency, bar.by_currency
    comparison.trade_count_delta = l1.trade_count - bar.trade_count
    comparison.by_currency_delta = {
        currency: CurrencyResult(**{
            metric: decimal_from_finite_fraction(
                Fraction(getattr(l1_amounts.get(currency), metric, 0))
                - Fraction(getattr(bar_amounts.get(currency), metric, 0)),
            )
            for metric in CurrencyResult.model_fields
        })
        for currency in sorted(l1_amounts.keys() | bar_amounts.keys())
    }
    return comparison


def run_replays(
    scheme_paths: Sequence[Path], data_dir: Path, *,
    windows: Sequence[tuple[int, int]] = (), workers: int = 1,
) -> ReplayReport:
    """每个方案和区间组合运行一次，省略区间时使用各方案的原始范围。"""
    if not scheme_paths:
        raise SchemeError("至少需要一个交易方案")
    if workers < 1:
        raise SchemeError("--workers 必须大于 0")
    data_dir = data_dir.expanduser().resolve()
    indicator_defaults = sdk.indicator_defaults(data_dir=data_dir)
    jobs = []
    comparison_pairs = []
    tick_instruments = set()
    scheme_configs: dict[Path, RunConfig] = {}
    code_versions: dict[Path, FilePluginCode] = {}
    overrides = [{"start": start, "end": end} for start, end in windows] or [{}]
    for path in scheme_paths:
        path = path.expanduser().resolve()
        config = scheme_configs.get(path)
        if config is None:
            config = resolve_run_config(path, data_dir, deterministic_only=True, indicator_defaults=indicator_defaults)
            scheme_configs[path] = config
            for code in (config.file_plugins or {}).values():
                if code_versions.setdefault(code.path, code) != code:
                    raise SchemeError(f"{code.path}：批次预检期间策略代码哈希发生变化")
        selection = config.scheme.slots.selection(Slot.DATA)
        if selection.reference != "parquet_replay":
            raise SchemeError(f"{path}：回放须使用 parquet_replay 数据入口")
        for override in overrides:
            try:
                params = ReplayParams.model_validate({**selection.params, **override})
            except ValueError as exc:
                raise SchemeError(f"{path}：无效的 Parquet 回放参数\n{exc}") from exc
            slots = config.scheme.slots.model_copy(update={
                "data": PluginSelection(plugin="parquet_replay", params=params.model_dump(mode="json")),
            })
            scheme = config.scheme.model_copy(update={"slots": slots})
            jobs.append(_ReplayJob(path, config.model_copy(update={"scheme": scheme}), params))
            fill = slots.selection(Slot.FILL)
            if fill.reference == "l1_trade_sim":
                tick_instruments.add(params.instrument)
                comparison_pairs.append((len(jobs) - 1, len(jobs)))
                baseline_params = {key: value for key, value in fill.params.items() if key != "quote_max_age_ms"}
                baseline_slots = slots.model_copy(update={
                    "fill": PluginSelection(plugin="bar_sim_1m", params=baseline_params),
                })
                baseline = scheme.model_copy(update={"slots": baseline_slots})
                jobs.append(_ReplayJob(path, config.model_copy(update={"scheme": baseline}), params))
    snapshot = capture_replay_snapshot(
        data_dir, (job.params for job in jobs), tick_instruments=tick_instruments,
    )
    jobs = [replace(job, config=job.config.model_copy(update={"cold_snapshot": snapshot})) for job in jobs]
    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
        results = list(pool.map(_ReplayJob.run, jobs))
    return ReplayReport(
        runs=results, fill_comparisons=[_compare_fills(results[l1], results[bar]) for l1, bar in comparison_pairs],
    )
