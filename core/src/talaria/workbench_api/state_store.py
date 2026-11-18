"""协同与感知状态的唯一写者：单线程 SQLite + 单 asyncio 协程串行队列。

架构约定（docs/architecture.md §5.2、ADR-0006）：``state/collaboration_state.sqlite``
只允许 daemon 内一个写者；写操作排队到一个协程，逐条提交，退出时刷盘并
``PRAGMA wal_checkpoint(TRUNCATE)``。05 的 watchlist / insight / staged plan
可直接复用 ``events`` 与 ``meta`` 表。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from talaria.foundation.sqlite import (
    checkpoint_and_close,
    claim_writer,
    release_writer,
)

STATE_SUBDIR = Path("state")
STATE_DB = STATE_SUBDIR / "collaboration_state.sqlite"


def _now_ms() -> int:
    return int(time.time() * 1000)


class StateStore:
    """唯一写者持有的 SQLite 连接；所有方法必须在同一线程/协程内串行调用。"""

    def __init__(self, data_dir: Path | str, *, now_ms: Callable[[], int] = _now_ms) -> None:
        self._db_path = Path(data_dir) / STATE_DB
        self._now_ms = now_ms
        self._closed = False
        claim_writer(self._db_path)
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                self._db_path, isolation_level=None, check_same_thread=False
            )
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_ts INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    ts INTEGER NOT NULL
                );
                """
            )
        except BaseException:
            release_writer(self._db_path)
            raise

    def append_event(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        ts: int | None = None,
    ) -> int:
        cursor = self._connection.execute(
            "INSERT INTO events (kind, payload, ts) VALUES (?, ?, ?)",
            (
                kind,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                self._now_ms() if ts is None else ts,
            ),
        )
        return int(cursor.lastrowid or 0)

    def set_meta(self, key: str, value: Any, *, ts: int | None = None) -> None:
        self._connection.execute(
            """
            INSERT INTO meta (key, value, updated_ts) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value, updated_ts = excluded.updated_ts
            """,
            (
                key,
                json.dumps(value, ensure_ascii=False, sort_keys=True),
                self._now_ms() if ts is None else ts,
            ),
        )

    def get_meta(self, key: str) -> Any | None:
        row = self._connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            checkpoint_and_close(self._connection)
        finally:
            release_writer(self._db_path)


class _Request:
    __slots__ = ("operation", "future")

    def __init__(
        self,
        operation: Callable[[StateStore], Any],
        future: asyncio.Future[Any],
    ) -> None:
        self.operation = operation
        self.future = future


class StateWriterQueue:
    """把状态写操作汇聚到单协程；调用者等待对应 future，保证提交顺序与失败可见。"""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        now_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._store = StateStore(data_dir, now_ms=now_ms)
        self._queue: asyncio.Queue[_Request | object] = asyncio.Queue()
        self._sentinel = object()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("StateWriterQueue 已关闭")
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def _submit(self, operation: Callable[[StateStore], Any]) -> Any:
        if self._closed:
            raise RuntimeError("StateWriterQueue 已关闭")
        if self._task is None:
            raise RuntimeError("StateWriterQueue 尚未启动")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(_Request(operation, future))
        return await future

    async def submit(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        ts: int | None = None,
    ) -> int:
        return await self._submit(
            lambda store: store.append_event(kind, payload, ts=ts)
        )

    async def set_meta(self, key: str, value: Any, *, ts: int | None = None) -> None:
        await self._submit(lambda store: store.set_meta(key, value, ts=ts))

    async def get_meta(self, key: str) -> Any | None:
        return await self._submit(lambda store: store.get_meta(key))

    async def _run(self) -> None:
        try:
            while True:
                request = await self._queue.get()
                try:
                    if request is self._sentinel:
                        return
                    assert isinstance(request, _Request)
                    try:
                        result = request.operation(self._store)
                    except Exception as error:  # 交给调用者
                        if not request.future.cancelled():
                            request.future.set_exception(error)
                    else:
                        if not request.future.cancelled():
                            request.future.set_result(result)
                finally:
                    self._queue.task_done()
        finally:
            self._store.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is None:
            self._store.close()
            return
        await self._queue.join()
        self._queue.put_nowait(self._sentinel)
        await self._task

    async def __aenter__(self) -> StateWriterQueue:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
