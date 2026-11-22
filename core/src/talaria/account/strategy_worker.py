"""受限 Python 策略的子进程入口。"""

from collections.abc import Callable
import ctypes
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
import os
from pathlib import Path
import sys

from pydantic import TypeAdapter

from talaria.account.parameters import validate_parameters
from talaria.account.scheme import SchemeError, Slot, check_plugin, load_python_module
from talaria.account.strategy_ipc import HOST_MESSAGE_LIMIT, WORKER_MESSAGE_LIMIT, StrategyPipe
from talaria.account.strategy_limits import limit_resources
from talaria.components import ComponentMeta, ControllerPatch
from talaria.foundation.bars import RangeBar
from talaria.foundation.collaboration.contracts import Abstain, SetupInsight
from talaria.foundation.derivatives import DerivativePoint
from talaria.foundation.indicators import IndicatorPoint
from talaria.foundation.operators import MAX_PERIOD


def apply_seatbelt(data_dir: str, read_dirs: list[str], deny_dirs: list[str]) -> None:
    names = [f"READ_DIR_{index}" for index in range(len(read_dirs))]
    reads = "\n".join(f'(allow file-read* (subpath (param "{name}")))' for name in names)
    excluded = [f"DENY_DIR_{index}" for index in range(len(deny_dirs))]
    denials = "\n".join(f'(deny file-read* (subpath (param "{name}")))' for name in excluded)
    # 数据目录可以位于代码目录下，最后匹配的拒绝规则优先。
    profile = f"""\
(version 1)
(deny default)
{reads}
{denials}
(deny file-read* file-write* (subpath (param "DATA_DIR")))
(allow file-write-data (literal "/dev/null"))
"""
    parameters = {"DATA_DIR": data_dir, **dict(zip(names, read_dirs)), **dict(zip(excluded, deny_dirs))}
    values = [os.fsencode(value) for pair in parameters.items() for value in pair]
    arguments = (ctypes.c_char_p * (len(values) + 1))(*values, None)
    lib = ctypes.CDLL("/usr/lib/libsandbox.dylib")
    install = lib.sandbox_init_with_parameters
    install.argtypes = (ctypes.c_char_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_char_p),
                        ctypes.POINTER(ctypes.c_char_p))
    install.restype = ctypes.c_int
    lib.sandbox_free_error.argtypes = (ctypes.c_char_p,)
    lib.sandbox_free_error.restype = None
    error = ctypes.c_char_p()
    if install(profile.encode(), 0, arguments, ctypes.byref(error)) != 0:
        detail = error.value.decode(errors="replace") if error.value else "未知错误"
        if error.value:
            lib.sandbox_free_error(error)
        raise SchemeError(f"无法启用 macOS Seatbelt：{detail}")


@dataclass(frozen=True, slots=True)
class _StrategyContext:
    mode: str
    seed: int
    now: Callable[[], int]


@dataclass(frozen=True, slots=True)
class _RemoteCtx:
    instrument: str
    timeframe: str
    start: int
    now: int
    _id: str
    _pipe: StrategyPipe

    def _read(self, operation: str, **params):
        self._pipe.send({"id": self._id, "read": operation, "params": params})
        reply = self._pipe.receive()
        if reply.get("id") != self._id:
            raise ValueError("策略 IPC 协议错误：取数回复 ID 不符")
        if "error" in reply:
            raise ValueError(reply["error"])
        return reply["data"]

    def bars(self, timeframe: str | None = None) -> tuple[RangeBar, ...]:
        return tuple(RangeBar.model_validate(bar) for bar in self._read("bars", timeframe=timeframe))

    def derivatives(self, kind: str) -> tuple[DerivativePoint, ...]:
        return tuple(DerivativePoint.model_validate(point) for point in self._read("derivatives", kind=kind))

    def indicator(self, name: str, *, timeframe: str | None = None, version: str = "1", **params):
        period = params.get("period")
        if isinstance(period, Decimal) and period.is_finite() and 1 <= period <= MAX_PERIOD:
            if period == period.to_integral_value():
                params["period"] = int(period)
        points = self._read("indicator", name=name, timeframe=timeframe, version=version, **params)
        return tuple(IndicatorPoint.model_validate(point) for point in points)

    @property
    def tail_completed(self) -> bool:
        return self._read("tail_completed")


def run() -> None:
    pipe = StrategyPipe(0, 1, receive_limit=HOST_MESSAGE_LIMIT, send_limit=WORKER_MESSAGE_LIMIT)
    sys.stdout = sys.stderr
    envelope = {}
    try:
        boot = pipe.receive()
        limit_resources()
        apply_seatbelt(boot.pop("data_dir"), boot.pop("read_dirs"), boot.pop("deny_dirs"))
        pipe.send({"isolated": True})
        if pipe.receive() != {"load": True}:
            raise SchemeError("无效的策略加载消息")
        entry = Path(boot["entry"])
        sys.path.append(str(entry.parent))
        definition = getattr(load_python_module(entry, entry.read_bytes()), boot["symbol"])
        plugin = check_plugin(definition, Slot.STRATEGY, boot["version"])
        now = None

        def clock() -> int:
            if now is None:
                raise ValueError("数据入口尚未推进时钟")
            return now

        params = boot["params"]
        if plugin.parameters_schema is not None:
            validate_parameters(params, plugin.parameters_schema)
        strategy = plugin.create(_StrategyContext(boot["mode"], boot["seed"], clock), deepcopy(params))
        meta = getattr(strategy, "meta", None)
        if not isinstance(meta, ComponentMeta) or meta.output_type != "strategy_decision" or not callable(
            getattr(strategy, "on_bar", None)
        ):
            raise SchemeError("策略插件未实现接口")
        validate_parameters(params, meta.parameters_schema)
        pipe.send({"ready": True})
        patch = TypeAdapter(ControllerPatch)
        while True:
            try:
                request = pipe.receive()
            except EOFError:
                return
            if request.keys() == {"sync"}:
                pipe.send({"id": request["sync"], "synced": True})
                continue
            callback_id = request["id"]
            envelope = {"id": callback_id}
            view = request["ctx"]
            now = view["now"]
            decision = strategy.on_bar(_RemoteCtx(**view, _pipe=pipe, _id=callback_id))
            if isinstance(decision, (SetupInsight, Abstain)):
                pipe.send({**envelope, "decision": type(decision).__name__, "data": decision.model_dump(mode="json")})
            elif isinstance(decision, ControllerPatch):
                pipe.send({**envelope, "decision": "ControllerPatch", "data": patch.dump_python(decision, mode="json")})
            else:
                raise ValueError("无效的策略输出")
            envelope = {}
    except MemoryError:
        pipe.send({**envelope, "error": "策略超过内存上限"})
    except Exception as exc:
        pipe.send({**envelope, "error": f"{type(exc).__name__}：{str(exc)[:1000]}"})
