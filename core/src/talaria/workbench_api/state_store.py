"""协同与感知状态的唯一写者：单线程 SQLite + 单 asyncio 协程串行队列。

架构约定（docs/architecture.md §5.2、ADR-0006）：``state/collaboration_state.sqlite``
只允许 daemon 内一个写者；写操作排队到一个协程，逐条提交，退出时刷盘并
``PRAGMA wal_checkpoint(TRUNCATE)``。05 的 watchlist / insight / staged plan
可直接复用 ``events`` 与 ``meta`` 表；任务单 05 的人工画线使用
``drawings``（当前版本）与 ``drawing_versions``（完整版本链）两张表。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from talaria.foundation.drawings import (
    DrawingCreate,
    DrawingPatch,
    DrawingPoint,
    DrawingRecord,
    DrawingStyle,
    visible_in_window,
)
from talaria.foundation.errors import DrawingNotFoundError
from talaria.foundation.sqlite import (
    checkpoint_and_close,
    claim_writer,
    release_writer,
)

STATE_SUBDIR = Path("state")
STATE_DB = STATE_SUBDIR / "collaboration_state.sqlite"

#: ``drawings`` 与 ``drawing_versions`` 共用的列顺序，读取时按同一顺序解包。
_DRAWING_COLUMNS = (
    "drawing_id",
    "instrument_id",
    "kind",
    "visible_timeframes",
    "points",
    "text",
    "style",
    "version",
    "created_ts",
    "updated_ts",
    "deleted_at",
)
_DRAWING_SELECT = ", ".join(_DRAWING_COLUMNS)

T = TypeVar("T")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _drawing_row(record: DrawingRecord) -> tuple[Any, ...]:
    """把契约对象编码成 SQLite 行；价格保持十进制字符串，不写 JSON number。"""

    return (
        record.drawing_id,
        record.instrument_id,
        record.kind.value,
        json.dumps(record.visible_timeframes, ensure_ascii=False),
        json.dumps(
            [point.model_dump(mode="json") for point in record.points],
            ensure_ascii=False,
        ),
        record.text,
        json.dumps(record.style.model_dump(mode="json"), ensure_ascii=False),
        record.version,
        record.created_ts,
        record.updated_ts,
        record.deleted_at,
    )


def _drawing_from_row(row: tuple[Any, ...]) -> DrawingRecord:
    (
        drawing_id,
        instrument_id,
        kind,
        visible_timeframes,
        points,
        text,
        style,
        version,
        created_ts,
        updated_ts,
        deleted_at,
    ) = row
    return DrawingRecord(
        drawing_id=drawing_id,
        instrument_id=instrument_id,
        kind=kind,
        visible_timeframes=json.loads(visible_timeframes),
        points=[DrawingPoint(**item) for item in json.loads(points)],
        text=text,
        style=DrawingStyle(**json.loads(style)),
        version=version,
        created_ts=created_ts,
        updated_ts=updated_ts,
        deleted_at=deleted_at,
    )


def _record_from_create(
    request: DrawingCreate, *, drawing_id: str, ts: int
) -> DrawingRecord:
    return DrawingRecord(
        drawing_id=drawing_id,
        instrument_id=request.instrument_id,
        kind=request.kind,
        visible_timeframes=list(request.visible_timeframes),
        points=request.points,
        text=request.text,
        style=request.style,
        version=1,
        created_ts=ts,
        updated_ts=ts,
    )


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
                CREATE TABLE IF NOT EXISTS drawings (
                    drawing_id TEXT PRIMARY KEY,
                    instrument_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    visible_timeframes TEXT NOT NULL,
                    points TEXT NOT NULL,
                    text TEXT NOT NULL,
                    style TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_ts INTEGER NOT NULL,
                    updated_ts INTEGER NOT NULL,
                    deleted_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS drawings_by_instrument
                    ON drawings (instrument_id, deleted_at);
                CREATE TABLE IF NOT EXISTS drawing_versions (
                    drawing_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    instrument_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    visible_timeframes TEXT NOT NULL,
                    points TEXT NOT NULL,
                    text TEXT NOT NULL,
                    style TEXT NOT NULL,
                    created_ts INTEGER NOT NULL,
                    updated_ts INTEGER NOT NULL,
                    deleted_at INTEGER,
                    PRIMARY KEY (drawing_id, version)
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

    # ---------------------------------------------------------------- 人工画线

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """显式事务：当前版本与版本链必须在同一次提交里可见。"""

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    def _insert_drawing_version(self, record: DrawingRecord) -> None:
        self._connection.execute(
            f"""
            INSERT INTO drawing_versions ({_DRAWING_SELECT})
            VALUES ({", ".join("?" for _ in _DRAWING_COLUMNS)})
            """,
            _drawing_row(record),
        )

    def _write_latest(self, record: DrawingRecord) -> None:
        self._connection.execute(
            f"""
            INSERT INTO drawings ({_DRAWING_SELECT})
            VALUES ({", ".join("?" for _ in _DRAWING_COLUMNS)})
            ON CONFLICT(drawing_id) DO UPDATE SET
                instrument_id = excluded.instrument_id,
                kind = excluded.kind,
                visible_timeframes = excluded.visible_timeframes,
                points = excluded.points,
                text = excluded.text,
                style = excluded.style,
                version = excluded.version,
                created_ts = excluded.created_ts,
                updated_ts = excluded.updated_ts,
                deleted_at = excluded.deleted_at
            """,
            _drawing_row(record),
        )
        self._insert_drawing_version(record)

    def create_drawing(self, request: DrawingCreate, *, drawing_id: str) -> DrawingRecord:
        ts = self._now_ms()
        record = _record_from_create(request, drawing_id=drawing_id, ts=ts)
        with self._transaction():
            self._write_latest(record)
        return record

    def get_drawing(self, drawing_id: str, *, version: int | None = None) -> DrawingRecord:
        if version is None:
            row = self._connection.execute(
                f"SELECT {_DRAWING_SELECT} FROM drawings WHERE drawing_id = ?",
                (drawing_id,),
            ).fetchone()
        else:
            row = self._connection.execute(
                f"""
                SELECT {_DRAWING_SELECT} FROM drawing_versions
                WHERE drawing_id = ? AND version = ?
                """,
                (drawing_id, version),
            ).fetchone()
        if row is None:
            raise DrawingNotFoundError(f"画线 {drawing_id} 不存在")
        return _drawing_from_row(row)

    def list_drawing_versions(self, drawing_id: str) -> list[DrawingRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_DRAWING_SELECT} FROM drawing_versions
            WHERE drawing_id = ? ORDER BY version
            """,
            (drawing_id,),
        ).fetchall()
        if not rows:
            raise DrawingNotFoundError(f"画线 {drawing_id} 不存在")
        return [_drawing_from_row(row) for row in rows]

    def update_drawing(
        self, drawing_id: str, patch: DrawingPatch, *, ts: int | None = None
    ) -> DrawingRecord:
        current = self.get_drawing(drawing_id)
        if current.deleted_at is not None:
            raise DrawingNotFoundError(f"画线 {drawing_id} 已删除")
        updated_ts = self._now_ms() if ts is None else ts
        updated = current.model_copy(
            update={
                "visible_timeframes": (
                    current.visible_timeframes
                    if patch.visible_timeframes is None
                    else patch.visible_timeframes
                ),
                "points": current.points if patch.points is None else patch.points,
                "text": current.text if patch.text is None else patch.text,
                "style": current.style if patch.style is None else patch.style,
                "version": current.version + 1,
                "updated_ts": updated_ts,
            }
        )
        with self._transaction():
            self._write_latest(updated)
        return updated

    def soft_delete_drawing(
        self, drawing_id: str, *, ts: int | None = None
    ) -> DrawingRecord:
        current = self.get_drawing(drawing_id)
        if current.deleted_at is not None:
            return current
        deleted_ts = self._now_ms() if ts is None else ts
        deleted = current.model_copy(
            update={
                "version": current.version + 1,
                "updated_ts": deleted_ts,
                "deleted_at": deleted_ts,
            }
        )
        with self._transaction():
            self._write_latest(deleted)
        return deleted

    def restore_drawing(
        self, drawing_id: str, *, ts: int | None = None
    ) -> DrawingRecord:
        current = self.get_drawing(drawing_id)
        if current.deleted_at is None:
            return current
        restored_ts = self._now_ms() if ts is None else ts
        restored = current.model_copy(
            update={
                "version": current.version + 1,
                "updated_ts": restored_ts,
                "deleted_at": None,
            }
        )
        with self._transaction():
            self._write_latest(restored)
        return restored

    def list_drawings(
        self,
        instrument_id: str,
        timeframe: str,
        *,
        from_ts: int | None = None,
        to_ts: int | None = None,
        price_min: Decimal | None = None,
        price_max: Decimal | None = None,
    ) -> list[DrawingRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_DRAWING_SELECT} FROM drawings
            WHERE instrument_id = ? AND deleted_at IS NULL
            ORDER BY created_ts, drawing_id
            """,
            (instrument_id,),
        ).fetchall()
        records = [_drawing_from_row(row) for row in rows]
        return [
            record
            for record in records
            if visible_in_window(
                record,
                timeframe=timeframe,
                from_ts=from_ts,
                to_ts=to_ts,
                price_min=price_min,
                price_max=price_max,
            )
        ]

    def clear_drawings(self, instrument_id: str, *, ts: int | None = None) -> int:
        rows = self._connection.execute(
            "SELECT drawing_id FROM drawings WHERE instrument_id = ? AND deleted_at IS NULL",
            (instrument_id,),
        ).fetchall()
        deleted_ts = self._now_ms() if ts is None else ts
        with self._transaction():
            for (drawing_id,) in rows:
                current = self.get_drawing(drawing_id)
                deleted = current.model_copy(
                    update={
                        "version": current.version + 1,
                        "updated_ts": deleted_ts,
                        "deleted_at": deleted_ts,
                    }
                )
                self._write_latest(deleted)
        return len(rows)

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

    async def run(self, operation: Callable[[StateStore], T]) -> T:
        """在单写协程上执行任意 Store 操作；读写都排队，避免跨连接并发。"""

        return await self._submit(operation)

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
