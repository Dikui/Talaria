"""账户宿主的策略隔离与按时点取数代理。"""

from collections.abc import Callable, Iterator
from concurrent.futures import wait
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
import re
import subprocess
import sys
import sysconfig
from threading import Event, Lock, Thread
from time import monotonic
from uuid import uuid4

from pydantic import TypeAdapter

from talaria.account.execution import AccountProcessError
from talaria.account.scheme import Mode, SchemeError, Slot, SlotPlugin
from talaria.account.strategy_ipc import HOST_MESSAGE_LIMIT, WORKER_MESSAGE_LIMIT, StrategyPipe
from talaria.account.strategy_limits import CPU_SECONDS, MAX_RSS_BYTES, TaskUsage, task_usage
from talaria.account.strategy_reads import ContextReads, StrategyIO
from talaria.components import ComponentMeta, ControllerPatch, Ctx, Strategy, StrategyDecision
from talaria.foundation.bars import RangeBar
from talaria.foundation.collaboration.contracts import Abstain, SetupInsight
from talaria.foundation.types import DECIMAL_PATTERN

STARTUP_SECONDS = 10
CALLBACK_SECONDS = 2
MAX_CONTEXT_READS = 64

_CORE_SOURCE = Path(__file__).resolve().parents[2]
_DECISION = {
    "SetupInsight": TypeAdapter(SetupInsight),
    "Abstain": TypeAdapter(Abstain),
    "ControllerPatch": TypeAdapter(ControllerPatch),
}


def isolated_strategy_plugin(entry: Path, original_dir: Path, symbol: str, version: int, lifetime: ExitStack) -> SlotPlugin:
    def create(context, params):
        return lifetime.enter_context(
            _open_worker(entry, original_dir, symbol, version, context.mode, context.seed, params, context.data_dir),
        )

    return SlotPlugin(Slot.STRATEGY, (version,), create)


def worker_command() -> list[str]:
    # 跳过 PYTHONPATH、用户 site、.pth 与工作目录。只加载此宿主的代码及安装依赖。
    installed_paths = sysconfig.get_paths()
    imports = [str(_CORE_SOURCE), installed_paths["purelib"], installed_paths["platlib"]]
    bootstrap = f"import sys; sys.path.extend({imports!r}); from talaria.account import strategy_worker as worker; worker.run()"
    return [sys.executable, "-I", "-S", "-B", "-c", bootstrap]


def _worker_read_dirs(snapshot: Path) -> list[str]:
    paths = sysconfig.get_paths()
    runtime = sysconfig.get_paths(vars={"base": sys.base_prefix, "platbase": sys.base_exec_prefix})
    roots = [runtime["stdlib"], runtime["platstdlib"], paths["purelib"], paths["platlib"]]
    return sorted({str(Path(root).resolve()) for root in [*roots, _CORE_SOURCE / "talaria", snapshot]})


@contextmanager
def _open_worker(
    entry: Path, original_dir: Path, symbol: str, version: int, mode: Mode, seed: int, params: dict, data_dir: Path,
) -> Iterator[Strategy]:
    if sys.platform != "darwin":
        raise SchemeError("agent 策略需要 macOS Seatbelt")
    read_dirs = _worker_read_dirs(entry.parent)
    base = sysconfig.get_paths(vars={"base": sys.base_prefix, "platbase": sys.base_exec_prefix})
    # 某些 Python 将全局第三方包放在标准库内部，仅保留当前环境选中的 site-packages。
    denied = {str(Path(base[key]).resolve()) for key in ("purelib", "platlib")} - set(read_dirs)
    if any(original_dir.is_relative_to(root) for root in read_dirs):
        denied.add(str(original_dir))
    with subprocess.Popen(
        worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={}, cwd="/", close_fds=True, start_new_session=True,
    ) as worker, _watch_worker(worker) as budget:
        try:
            pipe = StrategyPipe(
                worker.stdout.fileno(), worker.stdin.fileno(),
                receive_limit=WORKER_MESSAGE_LIMIT, send_limit=HOST_MESSAGE_LIMIT,
            )
            deadline = monotonic() + STARTUP_SECONDS
            try:
                pipe.send({
                    "entry": str(entry), "symbol": symbol, "version": version,
                    "mode": mode, "seed": seed, "params": params, "data_dir": str(data_dir),
                    "read_dirs": read_dirs, "deny_dirs": sorted(denied),
                }, deadline)
                if budget.receive(pipe, deadline) != {"isolated": True}:
                    raise SchemeError("无效的策略隔离就绪消息")
                budget.start_resource_budget()
                pipe.send({"load": True}, deadline)
                message = budget.receive(pipe, deadline)
            except TimeoutError as exc:
                raise SchemeError("策略子进程启动超时") from exc
            if message.keys() != {"ready"} or message["ready"] is not True:
                raise SchemeError("无效的策略子进程就绪消息")
            budget.start_callbacks(pipe)
            yield IsolatedStrategy(pipe, budget)
        finally:
            worker.kill()


@contextmanager
def _watch_worker(worker: subprocess.Popen):
    budget = _WorkerBudget(worker)
    monitor = Thread(target=budget.watch, name="talaria-strategy-budget", daemon=True)
    monitor.start()
    try:
        yield budget
    finally:
        budget.io.close()
        budget.stop.set()
        monitor.join()


class _WorkerBudget:
    def __init__(self, worker: subprocess.Popen) -> None:
        self.worker = worker
        self.io = StrategyIO()
        self.stop = Event()
        self._lock = Lock()
        self._baseline: TaskUsage | None = None
        self._pipe: StrategyPipe | None = None
        self._deadline: float | None = None
        self.error: str | None = None

    def start_resource_budget(self) -> None:
        # 子进程此刻已装入可信运行时、启用 Seatbelt，尚未读取策略代码。
        with self._lock:
            self._baseline = task_usage(self.worker.pid)

    def start_callbacks(self, pipe: StrategyPipe) -> None:
        with self._lock:
            self._pipe = pipe

    def _check_idle(self) -> None:
        # 只有回调持有的读端可以消费消息；空闲时任何输入（包括 EOF）都终止运行。
        if self.error is None and self._pipe is not None and self._deadline is None and self._pipe.readable():
            self.error = "策略 IPC 协议错误：回调外收到回复或子进程提前退出"
            self.worker.kill()

    def check_alive(self) -> None:
        with self._lock:
            self._check_idle()
            if self.error is not None or self.worker.poll() is not None:
                self.io.cancelled.set()
                raise AccountProcessError(self.error or "策略子进程提前退出")

    @contextmanager
    def callback(self) -> Iterator[float]:
        self.check_alive()
        deadline = monotonic() + CALLBACK_SECONDS
        with self._lock:
            self._deadline = deadline
        try:
            yield deadline
        finally:
            with self._lock:
                self._deadline = None

    def watch(self) -> None:
        try:
            self._watch_resources()
        finally:
            self.io.cancelled.set()

    def _watch_resources(self) -> None:
        # macOS 的 SIGXCPU 可捕获。宿主独立检查进程用量，包括两次回调之间。
        while not self.stop.wait(0.05) and self.worker.poll() is None:
            try:
                usage = task_usage(self.worker.pid)
            except OSError:
                # 退出时 task_info 可能先于 waitpid 失效。
                try:
                    self.worker.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    self.error = "无法监测策略子进程资源用量"
                    self.worker.kill()
                return
            with self._lock:
                self._check_idle()
                baseline = self._baseline
                if baseline is None:
                    continue
                if usage.resident > MAX_RSS_BYTES:
                    self.error = "策略超过驻留内存上限"
                elif usage.cpu_ns - baseline.cpu_ns >= CPU_SECONDS * 1_000_000_000:
                    self.error = "策略超过 CPU 上限"
                elif self._deadline is not None and monotonic() >= self._deadline:
                    self.error = "策略回调超时"
                if self.error is not None:
                    self.worker.kill()
                    return

    def io_result[T](self, operation: Callable[[], T], deadline: float | None = None) -> T:
        future = self.io.submit(operation)
        while True:
            self.check_alive()
            if deadline is not None and monotonic() >= deadline:
                raise TimeoutError
            if wait([future], timeout=0.02).done:
                self.check_alive()
                return future.result()

    def receive(self, pipe: StrategyPipe, deadline: float, request_id: str | None = None) -> dict:
        try:
            message = pipe.receive(deadline)
        except EOFError as exc:
            raise AccountProcessError(self.error or "策略子进程提前退出，可能已达资源上限") from exc
        if self.error is not None:
            raise AccountProcessError(self.error)
        if request_id is not None and message.pop("id", None) != request_id:
            raise AccountProcessError("策略 IPC 协议错误：回复缺失或不匹配当前请求 ID")
        if "error" in message:
            if message.keys() != {"error"} or not isinstance(message["error"], str):
                raise AccountProcessError("策略 IPC 协议错误：无效的错误消息")
            raise AccountProcessError(f"策略子进程失败：{message['error'][:1000]}")
        return message


class IsolatedStrategy:
    meta = ComponentMeta("isolated", "1", (), {}, "strategy_decision")

    def __init__(self, pipe: StrategyPipe, budget: _WorkerBudget) -> None:
        self._pipe = pipe
        self._budget = budget

    def verify_signal_bar(self, ctx: Ctx, bar: RangeBar) -> Ctx:
        def verify():
            signal = ContextReads(replace(ctx, start=bar.ts_event), self._budget.io.cancelled).ctx
            if signal.bars()[-1:] != (bar,):
                raise AccountProcessError("策略求值的行情与已确认柱不一致")
            return replace(ctx, _discovery_tail_completed=signal.tail_completed)

        try:
            return self._budget.io_result(verify, monotonic() + CALLBACK_SECONDS)
        except TimeoutError as exc:
            self._budget.io.cancelled.set()
            self._budget.worker.kill()
            raise AccountProcessError("策略行情核验取数超时") from exc

    def market_events(self, data):
        events = self._budget.io_result(lambda: iter(data.events()))
        end = object()
        while (market := self._budget.io_result(lambda: next(events, end))) is not end:
            yield market

    def on_bar(self, ctx: Ctx) -> StrategyDecision:
        try:
            with self._budget.callback() as deadline:
                callback_id = uuid4().hex
                self._pipe.send({"id": callback_id, "ctx": {
                    "instrument": ctx.instrument, "timeframe": ctx.timeframe, "start": ctx.start, "now": ctx.now,
                }}, deadline)
                context = ContextReads(ctx, self._budget.io.cancelled)
                for reads in range(MAX_CONTEXT_READS + 1):
                    message = self._budget.receive(self._pipe, deadline, callback_id)
                    if message.keys() == {"decision", "data"}:
                        decision = _read_decision(message)
                        # 独立的同步请求确认前一回调已结束，再把决定交给风控。
                        sync_id = uuid4().hex
                        self._pipe.send({"sync": sync_id}, deadline)
                        if self._budget.receive(self._pipe, deadline, sync_id) != {"synced": True}:
                            raise AccountProcessError("策略 IPC 协议错误：回调结束后仍有额外回复")
                        return decision
                    if message.keys() != {"read", "params"}:
                        raise AccountProcessError("策略 IPC 协议错误：无效的回调消息")
                    if reads == MAX_CONTEXT_READS:
                        raise AccountProcessError("策略单次回调的 ctx 查询次数超出上限")
                    reply = self._budget.io_result(lambda: context.reply(message), deadline)
                    self._pipe.send({"id": callback_id, **reply}, deadline)
        except TimeoutError as exc:
            self._budget.io.cancelled.set()
            self._budget.worker.kill()
            raise AccountProcessError("策略回调超时") from exc
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            self._budget.io.cancelled.set()
            self._budget.worker.kill()
            if isinstance(exc, AccountProcessError):
                raise
            raise AccountProcessError(self._budget.error or f"无效的策略子进程消息：{exc}") from exc


def _read_decision(message: dict) -> StrategyDecision:
    kind, data = message["decision"], message["data"]
    if not isinstance(data, dict):
        raise AccountProcessError("策略输出须为 JSON 对象")
    if kind == "SetupInsight":
        # 在构造 Decimal 前拒绝科学计数法。宿主后续会对这些价格做算术和序列化。
        for field in ("entry_low", "entry_high", "invalidation_stop", "max_position", "take_profit"):
            values = data.get(field)
            for value in values if field == "take_profit" and isinstance(values, list) else [values]:
                if value is None and field == "max_position":
                    continue
                if not isinstance(value, str) or not re.fullmatch(DECIMAL_PATTERN, value):
                    raise AccountProcessError(f"策略输出的 {field} 必须是十进制字符串")
    return _DECISION[kind].validate_python(data)
