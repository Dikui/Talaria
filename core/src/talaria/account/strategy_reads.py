"""策略 IPC 使用的有界、可取消的数据读取。"""

from collections.abc import Callable
from concurrent.futures import Future
from contextvars import ContextVar
from dataclasses import replace
import json
from math import isfinite
from queue import SimpleQueue
from threading import BoundedSemaphore, Event, Thread

from talaria.account.execution import AccountProcessError
from talaria.components import Ctx
from talaria.foundation.timeframes import timeframe_ms

MAX_CONTEXT_ROWS = 8192
MAX_CONTEXT_BYTES = 4 * 1024 * 1024
_ESTIMATED_ROW_BYTES = 512
# 即使外部同步读取器迟迟不返回，也限制本进程内仍占用资源的读取线程。
_IO_SLOTS = BoundedSemaphore(16)


_READ_CANCELLATION = ContextVar[Event]("talaria_read_cancellation")


def read_cancellation() -> Event:
    """供同步数据插件中断正在等待的行情或 I/O；仅在隔离策略读取线程中使用。"""
    return _READ_CANCELLATION.get()


class StrategyIO:
    """单账户串行读取。关闭即唤醒空闲线程，正在读取的任务在取消点退出。"""

    def __init__(self) -> None:
        self.cancelled = Event()
        self._requests = SimpleQueue()
        if not _IO_SLOTS.acquire(blocking=False):
            raise AccountProcessError("宿主策略取数线程已达上限")
        try:
            Thread(target=self._run, name="talaria-strategy-io", daemon=True).start()
        except BaseException:
            _IO_SLOTS.release()
            raise

    def submit[T](self, operation: Callable[[], T]) -> Future[T]:
        result: Future[T] = Future()
        self._requests.put((operation, result))
        return result

    def close(self) -> None:
        self.cancelled.set()
        self._requests.put(None)

    def _run(self) -> None:
        _READ_CANCELLATION.set(self.cancelled)
        try:
            while (request := self._requests.get()) is not None:
                operation, result = request
                try:
                    if self.cancelled.is_set():
                        raise AccountProcessError("策略取数已取消")
                    result.set_result(operation())
                except BaseException as exc:
                    result.set_exception(exc)
                del operation, result, request
        finally:
            _IO_SLOTS.release()


class ContextReads:
    def __init__(self, ctx: Ctx, cancelled: Event):
        self.cancelled = cancelled
        self.rows = 0
        self.ctx = replace(
            ctx, _read_bars=self._bounded(ctx._read_bars, bars=True),
            _read_derivatives=self._bounded(ctx._read_derivatives, bars=False) if ctx._read_derivatives else None,
            _read_derivative_revisions=(
                self._bounded_revisions(ctx._read_derivative_revisions)
                if ctx._read_derivative_revisions is not None else None
            ),
        )

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise AccountProcessError("策略取数已取消")

    def _count_rows(self, rows):
        self.check_cancelled()
        self.rows += len(rows)
        if self.rows > MAX_CONTEXT_ROWS:
            raise AccountProcessError("策略取数结果超过行数上限")
        return rows

    def _bounded_revisions(self, reader):
        def read(kind):
            # 宿主固定的衍生品历史可以很稀疏，按实际返回的修订计数。
            self.check_cancelled()
            return self._count_rows(reader(kind))
        return read

    def _bounded(self, reader, *, bars):
        def read(instrument, series, *, start, end, as_of):
            self.check_cancelled()
            # Ctx 已将预热窗口、指标依赖和衍生品前序历史展开为底层查询范围。
            interval = timeframe_ms(series) if bars else 60_000
            estimate = max(0, (end - start) // interval + 1)
            if self.rows + estimate > MAX_CONTEXT_ROWS:
                raise AccountProcessError("策略取数范围超过行数上限")
            if (self.rows + estimate) * _ESTIMATED_ROW_BYTES > MAX_CONTEXT_BYTES:
                raise AccountProcessError("策略取数估算超过字节上限")
            return self._count_rows(reader(instrument, series, start=start, end=end, as_of=as_of))
        return read

    def reply(self, message: dict) -> dict:
        self.check_cancelled()
        self.rows = 0
        method, params = message["read"], message["params"]
        if not isinstance(params, dict) or any(
            type(value) not in (str, int, float, bool, type(None))
            or (isinstance(value, str) and len(value) > 1024)
            or (isinstance(value, float) and not isfinite(value))
            for value in params.values()
        ):
            raise AccountProcessError("无效的策略 ctx 查询参数")
        if method == "tail_completed" and not params:
            return {"data": self.ctx.tail_completed}
        read = {"bars": self.ctx.bars, "derivatives": self.ctx.derivatives, "indicator": self.ctx.indicator}.get(method)
        if read is None:
            raise AccountProcessError("未知的策略 ctx 查询")
        try:
            points = read(**params)
            if len(points) > MAX_CONTEXT_ROWS:
                raise AccountProcessError("策略取数结果超过行数上限")
            data = []
            size = 0
            for point in points:
                self.check_cancelled()
                row = point.model_dump(mode="json")
                size += len(json.dumps(row, allow_nan=False)) + 2
                if size > MAX_CONTEXT_BYTES:
                    raise AccountProcessError("策略取数结果超过字节上限")
                data.append(row)
            return {"data": data}
        except AccountProcessError:
            raise
        except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
            return {"error": str(exc)[:1000]}
