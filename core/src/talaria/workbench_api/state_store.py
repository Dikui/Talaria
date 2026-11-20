"""协同状态库连接、领域仓储与单写队列。

架构约定（docs/architecture.md §5.2、ADR-0006）：协程按提交顺序把写操作
交给专用线程，读操作使用独立的只读连接，退出时截断 WAL。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any, TypeVar

from talaria.perception.sqlite import (
    checkpoint_and_close,
    claim_writer,
    release_writer,
)
from talaria.workbench_api.state_repositories import (
    alerts,
    annotations,
    briefings,
    consultations,
    drawings,
    key_level_feedback,
    meta,
    usage,
    watchlist,
)

STATE_SUBDIR = Path("state")
STATE_DB = STATE_SUBDIR / "collaboration_state.sqlite"

T = TypeVar("T")


def _now_ms() -> int:
    return int(time.time() * 1000)


class StateStore:
    """协同状态的 SQLite 连接；所有方法必须在打开连接的线程内调用。"""

    def __init__(self, data_dir: Path | str, *, now_ms: Callable[[], int] = _now_ms) -> None:
        db_path = (Path(data_dir) / STATE_DB).resolve()
        claim_writer(db_path)
        self._init_claimed_writer(db_path, now_ms)

    @classmethod
    def _open_claimed_writer(
        cls, db_path: Path, now_ms: Callable[[], int]
    ) -> StateStore:
        store = cls.__new__(cls)
        store._init_claimed_writer(db_path, now_ms)
        return store

    def _init_claimed_writer(self, db_path: Path, now_ms: Callable[[], int]) -> None:
        self._writer_path: Path | None = db_path
        self._closed = False
        connection = None
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(db_path, isolation_level=None)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            for schema in (
                meta.SCHEMA,
                drawings.SCHEMA,
                watchlist.SCHEMA,
                key_level_feedback.SCHEMA,
                alerts.SCHEMA,
                annotations.SCHEMA,
                consultations.SCHEMA,
                briefings.SCHEMA,
                usage.SCHEMA,
            ):
                connection.executescript(schema)
            self._bind_repositories(connection, now_ms)
        except BaseException:
            try:
                if connection is not None:
                    connection.close()
            finally:
                release_writer(db_path)
            raise

    def _bind_repositories(
        self, connection: sqlite3.Connection, now_ms: Callable[[], int]
    ) -> None:
        self._connection = connection
        self.meta = meta.MetaRepository(connection, now_ms)
        self.drawings = drawings.DrawingRepository(connection, now_ms)
        self.watchlist = watchlist.WatchlistRepository(connection, now_ms)
        self.key_level_feedback = key_level_feedback.KeyLevelFeedbackRepository(
            connection, now_ms, self.meta
        )
        self.alerts = alerts.AlertRepository(connection, now_ms)
        self.annotations = annotations.AnnotationRepository(connection, now_ms)
        self.consultations = consultations.ConsultationRepository(connection)
        self.briefings = briefings.BriefingRepository(connection)
        self.usage = usage.UsageRepository(connection, now_ms)

    @classmethod
    def _read(
        cls,
        db_path: Path,
        now_ms: Callable[[], int],
        operation: Callable[[StateStore], T],
    ) -> T:
        db_uri = f"{db_path.as_uri()}?mode=ro"
        with closing(sqlite3.connect(db_uri, uri=True, isolation_level=None)) as connection:
            connection.execute("PRAGMA query_only=ON")
            # 一次读取中的多条查询共享同一个 WAL 快照。
            connection.execute("BEGIN")
            store = cls.__new__(cls)
            store._writer_path = None
            store._closed = False
            store._bind_repositories(connection, now_ms)
            return operation(store)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._writer_path is None:
            self._connection.close()
            return
        try:
            checkpoint_and_close(self._connection)
        finally:
            release_writer(self._writer_path)


class _Request:
    __slots__ = ("operation", "future")

    def __init__(
        self,
        operation: Callable[[StateStore], Any],
        future: asyncio.Future[Any],
    ) -> None:
        self.operation = operation
        self.future = future

    def execute(self, store: StateStore) -> tuple[Any, BaseException | None]:
        try:
            return self.operation(store), None
        except (Exception, asyncio.CancelledError) as error:
            # 操作抛出的取消也只交给该请求，不能取消派发协程。
            return None, error


class StateWriterQueue:
    """由单协程按提交顺序派发到写线程，调用者等待各自的结果。"""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        now_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._db_path = (Path(data_dir) / STATE_DB).resolve()
        # 构造时拒绝第二个写者，SQLite 的打开与初始化留给启动时的写线程。
        claim_writer(self._db_path)
        self._now_ms = now_ms
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="state-writer"
        )
        self._queue: asyncio.Queue[_Request | None] = asyncio.Queue()
        self._reads: set[asyncio.Future[Any]] = set()
        self._starting: asyncio.Task[None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._closing is not None:
            raise RuntimeError("StateWriterQueue 已关闭")
        if self._starting is None:
            self._starting = asyncio.create_task(self._start())
        await asyncio.shield(self._starting)

    async def _start(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            store = await loop.run_in_executor(
                self._executor,
                StateStore._open_claimed_writer,
                self._db_path,
                self._now_ms,
            )
        except Exception:
            await asyncio.to_thread(self._executor.shutdown)
            raise
        self._task = asyncio.create_task(self._run(store))

    def _require_running(self) -> None:
        if self._closing is not None:
            raise RuntimeError("StateWriterQueue 已关闭")
        if self._task is None:
            raise RuntimeError("StateWriterQueue 尚未启动")

    async def _submit(self, operation: Callable[[StateStore], Any]) -> Any:
        self._require_running()
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
            lambda store: store.meta.append_event(kind, payload, ts=ts)
        )

    async def set_meta(self, key: str, value: Any, *, ts: int | None = None) -> None:
        await self._submit(lambda store: store.meta.set(key, value, ts=ts))

    async def get_meta(self, key: str) -> Any | None:
        return await self.read(lambda store: store.meta.get(key))

    async def run(self, operation: Callable[[StateStore], T]) -> T:
        """在专用线程的写连接上串行执行状态操作。"""

        return await self._submit(operation)

    async def read(self, operation: Callable[[StateStore], T]) -> T:
        """在线程池的只读连接上查询已提交的数据。"""

        self._require_running()
        future = asyncio.get_running_loop().run_in_executor(
            None, StateStore._read, self._db_path, self._now_ms, operation
        )
        self._reads.add(future)
        future.add_done_callback(self._read_finished)
        return await asyncio.shield(future)

    def _read_finished(self, future: asyncio.Future[Any]) -> None:
        self._reads.discard(future)
        if not future.cancelled():
            # 调用者取消后，线程中的查询仍会结束并关闭连接。
            future.exception()

    async def _run(self, store: StateStore) -> None:
        loop = asyncio.get_running_loop()
        try:
            while (request := await self._queue.get()) is not None:
                result, error = await loop.run_in_executor(
                    self._executor, request.execute, store
                )
                if request.future.cancelled():
                    continue
                if error is None:
                    request.future.set_result(result)
                else:
                    request.future.set_exception(error)
        finally:
            try:
                await loop.run_in_executor(self._executor, store.close)
            finally:
                await asyncio.to_thread(self._executor.shutdown)

    async def close(self) -> None:
        if self._closing is None:
            self._closing = asyncio.create_task(self._finish_close())
        await asyncio.shield(self._closing)

    async def _finish_close(self) -> None:
        if self._starting is None:
            self._executor.shutdown()
            release_writer(self._db_path)
            return
        try:
            await self._starting
        except Exception:
            # 启动失败时已关闭执行器，打开连接失败时已释放写者登记。
            return
        assert self._task is not None
        # 先释放只读快照，再让写线程截断 WAL。
        await asyncio.gather(*self._reads, return_exceptions=True)
        self._queue.put_nowait(None)
        await self._task

    async def __aenter__(self) -> StateWriterQueue:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
