"""批量启动离线账户进程，按输入顺序汇总其账本。"""

from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Literal

from talaria.account.host import RunConfig, resolve_run_config
from talaria.account.ledger import read_journal
from talaria.account.plugins.parquet import ReplayParams
from talaria.account.scheme import PluginSelection, SchemeError, Slot
from talaria.foundation.types import ContractModel, DecimalStr, decimal_from_finite_fraction

_FEES = frozenset({"EXPENSE_EXECUTION_FEE", "EXPENSE_LIQUIDATION_FEE"})


class CurrencyResult(ContractModel):
    """同币种子账户的汇总金额，资金费为账户净收付，回撤为绝对金额。"""

    net_pnl: DecimalStr
    max_drawdown: DecimalStr
    fees: DecimalStr
    funding: DecimalStr


class ReplayResult(ContractModel):
    scheme: str
    scheme_path: Path
    start: int
    end: int
    run_id: str
    directory: Path
    trade_count: int
    by_currency: dict[str, CurrencyResult]


class ReplayReport(ContractModel):
    strategy_scope: Literal["deterministic_only"] = "deterministic_only"
    pnl_basis: Literal["booked"] = "booked"
    runs: list[ReplayResult]


@dataclass
class _BookedResult:
    net_pnl: Fraction = Fraction(0)
    peak_pnl: Fraction = Fraction(0)
    max_drawdown: Fraction = Fraction(0)
    fees: Fraction = Fraction(0)
    funding: Fraction = Fraction(0)

    def book(self, pnl: Fraction, fees: Fraction, funding: Fraction) -> None:
        self.fees += fees
        self.funding += funding
        self.net_pnl += pnl - fees + funding
        self.peak_pnl = max(self.peak_pnl, self.net_pnl)
        self.max_drawdown = max(self.max_drawdown, self.peak_pnl - self.net_pnl)

    def result(self) -> CurrencyResult:
        return CurrencyResult(
            net_pnl=decimal_from_finite_fraction(self.net_pnl),
            max_drawdown=decimal_from_finite_fraction(self.max_drawdown),
            fees=decimal_from_finite_fraction(self.fees),
            funding=decimal_from_finite_fraction(self.funding),
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
                -postings["REALIZED_PNL"], sum((postings[account] for account in _FEES), Fraction(0)),
                -postings["FUNDING"],
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
        # 配置中的 scheme_dir 保持相对 Python 引用指向原方案目录。
        with TemporaryDirectory(prefix="talaria-replay-") as temp:
            artifact = Path(temp) / "run-config.json"
            artifact.write_text(self.config.model_dump_json(), encoding="utf-8")
            process = subprocess.run(
                [sys.executable, "-m", "talaria", "account", "run", str(artifact),
                 "--mode", "backtest", "--replay"],
                capture_output=True, text=True, encoding="utf-8",
            )
        context = f"{self.scheme_path} [{self.params.start}, {self.params.end}]"
        if process.returncode:
            reason = process.stderr.strip() or f"账户进程退出码 {process.returncode}"
            raise SchemeError(f"{context}：{reason}")
        directory = Path(process.stdout.rstrip().rpartition("\n")[2])
        if directory.parent != self.config.data_dir / "runs":
            raise SchemeError(f"{context}：账户进程没有返回有效的回放目录")
        trades, by_currency = summarize_ledger(directory / "ledger.sqlite")
        return ReplayResult(
            scheme=self.config.scheme.scheme, scheme_path=self.scheme_path,
            start=self.params.start, end=self.params.end,
            run_id=directory.name, directory=directory, trade_count=trades, by_currency=by_currency,
        )


def run_replays(
    scheme_paths: Sequence[Path], data_dir: Path, *,
    windows: Sequence[tuple[int, int]] = (), workers: int = 1,
) -> ReplayReport:
    """每个方案和区间组合运行一次，省略区间时使用各方案的原始范围。"""
    if not scheme_paths:
        raise SchemeError("至少需要一个交易方案")
    if workers < 1:
        raise SchemeError("--workers 必须大于 0")
    jobs = []
    for path in scheme_paths:
        path = path.expanduser().resolve()
        config = resolve_run_config(path, data_dir, deterministic_only=True)
        selection = config.scheme.slots.selection(Slot.DATA)
        if selection.reference != "parquet_replay":
            raise SchemeError(f"{path}：回放须使用 parquet_replay 数据入口")
        overrides = [{"start": start, "end": end} for start, end in windows] or [{}]
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
    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
        return ReplayReport(runs=list(pool.map(_ReplayJob.run, jobs)))
