"""模拟盘宿主的策略隔离与按时点取数代理。"""

from base64 import b64encode
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from math import isfinite
import os
from pathlib import Path
import re
import site
import subprocess
import sys
import sysconfig
from threading import Event, Lock, Thread
from time import monotonic

from pydantic import TypeAdapter

from talaria.account.execution import AccountProcessError
from talaria.account.scheme import SchemeError, Slot, SlotPlugin, check_plugin, load_python_module, python_reference
from talaria.account.strategy_code import StrategyCodeRegistry
from talaria.account.strategy_ipc import HOST_MESSAGE_LIMIT, WORKER_MESSAGE_LIMIT, StrategyPipe
from talaria.account.strategy_limits import CPU_SECONDS, task_usage
from talaria.components import ComponentMeta, ControllerPatch, Ctx, Strategy, StrategyDecision
from talaria.foundation.collaboration.contracts import Abstain, SetupInsight
from talaria.foundation.types import DECIMAL_PATTERN

STARTUP_SECONDS = 10
CALLBACK_SECONDS = 2
MAX_CONTEXT_READS = 64

_CORE_SOURCE = Path(__file__).resolve().parents[2]
_META = TypeAdapter(ComponentMeta)
_DECISION = {
    "SetupInsight": TypeAdapter(SetupInsight),
    "Abstain": TypeAdapter(Abstain),
    "ControllerPatch": TypeAdapter(ControllerPatch),
}


def paper_strategy_plugin(
    reference: str, version: int, scheme_dir: Path, data_dir: Path, lifetime: ExitStack,
) -> SlotPlugin:
    try:
        path, symbol = python_reference(reference, scheme_dir)
        origin, code = StrategyCodeRegistry(data_dir).paper_source(path)
        if origin == "human":
            return check_plugin(getattr(load_python_module(path, code), symbol), Slot.STRATEGY, version)

        def create(context, params):
            return lifetime.enter_context(
                _open_worker(code, symbol, version, context.seed, params, data_dir, path.parent),
            )

        return SlotPlugin(Slot.STRATEGY, (version,), create)
    except Exception as exc:
        raise SchemeError(f"策略插槽插件 {reference}：{exc}") from exc


def worker_command() -> list[str]:
    # 跳过 PYTHONPATH、用户 site、.pth 与工作目录。只加载此宿主的代码及安装依赖。
    installed_paths = sysconfig.get_paths()
    imports = [str(_CORE_SOURCE), installed_paths["purelib"], installed_paths["platlib"]]
    bootstrap = f"import sys; sys.path.extend({imports!r}); from talaria.account import strategy_worker as worker; worker.run()"
    return [sys.executable, "-I", "-S", "-B", "-c", bootstrap]


def _worker_read_dirs(strategy_dir: Path) -> list[str]:
    installed_paths = sysconfig.get_paths()
    pythonpath = os.environ.get("PYTHONPATH", "").split(os.pathsep)
    directories = (
        sys.prefix, sys.base_prefix,
        *(installed_paths[name] for name in ("stdlib", "platstdlib", "purelib", "platlib")),
        *site.getsitepackages(),
        _CORE_SOURCE, strategy_dir,
        *(entry for entry in pythonpath if entry and Path(entry).is_dir()),
    )
    read_dirs = set()
    for directory in directories:
        path = Path(directory).absolute()
        read_dirs.update((str(path), str(path.resolve())))
    return sorted(read_dirs)


@contextmanager
def _open_worker(
    code: bytes, symbol: str, version: int, seed: int, params: dict, data_dir: Path, strategy_dir: Path,
) -> Iterator[Strategy]:
    if sys.platform != "darwin":
        raise SchemeError("agent 策略模拟盘需要 macOS Seatbelt")
    read_dirs = _worker_read_dirs(strategy_dir)
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
                    "code": b64encode(code).decode("ascii"), "symbol": symbol, "version": version,
                    "seed": seed, "params": params, "data_dir": str(data_dir), "read_dirs": read_dirs,
                }, deadline)
                message = budget.receive(pipe, deadline)
            except TimeoutError as exc:
                raise SchemeError("策略子进程启动超时") from exc
            yield _IsolatedStrategy(_META.validate_python(message["meta"]), pipe, budget)
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
        budget.stop.set()
        monitor.join()


class _WorkerBudget:
    def __init__(self, worker: subprocess.Popen) -> None:
        self.worker = worker
        self.stop = Event()
        self._lock = Lock()
        self._deadline: float | None = None
        self.error: str | None = None

    @contextmanager
    def callback(self) -> Iterator[float]:
        deadline = monotonic() + CALLBACK_SECONDS
        with self._lock:
            self._deadline = deadline
        try:
            yield deadline
        finally:
            with self._lock:
                self._deadline = None

    def watch(self) -> None:
        # macOS 的 SIGXCPU 可捕获。宿主独立检查进程用量，包括两次回调之间。
        while not self.stop.wait(0.05) and self.worker.poll() is None:
            try:
                _, cpu_ns = task_usage(self.worker.pid)
            except OSError:
                if self.worker.poll() is None:
                    self.error = "无法监测策略子进程资源用量"
                    self.worker.kill()
                return
            with self._lock:
                if cpu_ns >= CPU_SECONDS * 1_000_000_000:
                    self.error = "策略超过 CPU 上限"
                elif self._deadline is not None and monotonic() >= self._deadline:
                    self.error = "策略回调超时"
                if self.error is not None:
                    self.worker.kill()
                    return

    def receive(self, pipe: StrategyPipe, deadline: float) -> dict:
        try:
            message = pipe.receive(deadline)
        except EOFError as exc:
            raise AccountProcessError(self.error or "策略子进程提前退出，可能已达资源上限") from exc
        if self.error is not None:
            raise AccountProcessError(self.error)
        if "error" in message:
            raise AccountProcessError(f"策略子进程失败：{str(message['error'])[:1000]}")
        return message


class _IsolatedStrategy:
    def __init__(self, meta: ComponentMeta, pipe: StrategyPipe, budget: _WorkerBudget) -> None:
        self.meta = meta
        self._pipe = pipe
        self._budget = budget

    def on_bar(self, ctx: Ctx) -> StrategyDecision:
        try:
            with self._budget.callback() as deadline:
                self._pipe.send({"ctx": {
                    "instrument": ctx.instrument, "timeframe": ctx.timeframe, "start": ctx.start, "now": ctx.now,
                }}, deadline)
                for reads in range(MAX_CONTEXT_READS + 1):
                    message = self._budget.receive(self._pipe, deadline)
                    if "read" not in message:
                        return _read_decision(message)
                    if reads == MAX_CONTEXT_READS:
                        raise AccountProcessError("策略单次回调的 ctx 查询次数超出上限")
                    self._pipe.send(_read_context(ctx, message), deadline)
        except TimeoutError as exc:
            self._budget.worker.kill()
            raise AccountProcessError("策略回调超时") from exc
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            self._budget.worker.kill()
            if isinstance(exc, AccountProcessError):
                raise
            raise AccountProcessError(self._budget.error or f"无效的策略子进程消息：{exc}") from exc


def _read_context(ctx: Ctx, message: dict) -> dict:
    method, params = message["read"], message["params"]
    if not isinstance(params, dict) or any(
        type(value) not in (str, int, float, bool, type(None))
        or (isinstance(value, str) and len(value) > 1024)
        or (isinstance(value, float) and not isfinite(value))
        for value in params.values()
    ):
        raise AccountProcessError("无效的策略 ctx 查询参数")
    if method == "tail_completed" and not params:
        return {"data": ctx.tail_completed}
    read = {"bars": ctx.bars, "derivatives": ctx.derivatives, "indicator": ctx.indicator}.get(method)
    if read is None:
        raise AccountProcessError("未知的策略 ctx 查询")
    try:
        return {"data": [point.model_dump(mode="json") for point in read(**params)]}
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        return {"error": str(exc)[:1000]}


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
