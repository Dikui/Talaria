"""策略子进程的有界 JSON 管道。"""

from decimal import Decimal
import json
from math import ceil
import os
from select import POLLIN, POLLOUT, poll
from time import monotonic

HOST_MESSAGE_LIMIT = 16 * 1024 * 1024
WORKER_MESSAGE_LIMIT = 64 * 1024


def _encode_extra(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"不支持的策略消息值：{type(value).__name__}")


class StrategyPipe:
    def __init__(self, read_fd: int, write_fd: int, *, receive_limit: int, send_limit: int) -> None:
        self._read_fd, self._write_fd = read_fd, write_fd
        self._receive_limit, self._send_limit = receive_limit, send_limit
        self._pending = bytearray()
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)

    def send(self, message: dict, deadline: float | None = None) -> None:
        encoded = json.dumps(message, allow_nan=False, default=_encode_extra).encode() + b"\n"
        if len(encoded) > self._send_limit:
            raise ValueError("策略消息超出大小上限")
        remaining = memoryview(encoded)
        while remaining:
            self._wait(self._write_fd, POLLOUT, deadline)
            try:
                remaining = remaining[os.write(self._write_fd, remaining):]
            except BlockingIOError:
                continue

    def receive(self, deadline: float | None = None) -> dict:
        if deadline is not None and monotonic() >= deadline:
            raise TimeoutError
        line_end = self._pending.find(b"\n")
        while line_end == -1:
            length = len(self._pending)
            if length >= self._receive_limit:
                raise ValueError("策略消息超出大小上限")
            self._wait(self._read_fd, POLLIN, deadline)
            try:
                chunk = os.read(self._read_fd, min(65536, self._receive_limit - length))
            except BlockingIOError:
                continue
            if not chunk:
                raise EOFError("策略子进程管道已关闭")
            self._pending.extend(chunk)
            line_end = self._pending.find(b"\n", length)
        if deadline is not None and monotonic() >= deadline:
            raise TimeoutError
        message = json.loads(self._pending[:line_end])
        del self._pending[:line_end + 1]
        if not isinstance(message, dict):
            raise ValueError("策略消息须为 JSON 对象")
        return message

    @staticmethod
    def _wait(fd: int, event: int, deadline: float | None) -> None:
        timeout = None if deadline is None else ceil((deadline - monotonic()) * 1000)
        if timeout is not None and timeout <= 0:
            raise TimeoutError
        ready = poll()
        ready.register(fd, event)
        if not ready.poll(timeout):
            raise TimeoutError
